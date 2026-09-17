"""Lightweight async HTTP webhook server for TradingView / external alerts.

Listens on WEBHOOK_PORT (default 8080) when WEBHOOK_ENABLED is True.
Dispatches incoming webhook alerts directly to Telegram and ntfy push.
Zero external dependencies (uses standard library asyncio HTTP server).
"""

import asyncio
import json
import logging
import secrets
import urllib.parse

import config
from notifier import send_ntfy

logger = logging.getLogger(__name__)

_server = None


def _parse_http_request(raw_data: bytes) -> tuple[str, str, dict, str]:
    """Parse a basic HTTP request. Returns (method, path, headers, body)."""
    text = raw_data.decode("utf-8", errors="replace")
    if "\r\n\r\n" in text:
        parts = text.split("\r\n\r\n", 1)
        lines = parts[0].split("\r\n")
    else:
        parts = text.split("\n\n", 1)
        lines = parts[0].split("\n")
    body = parts[1] if len(parts) > 1 else ""
    if not lines or not lines[0]:
        return "", "", {}, ""

    request_line = lines[0].split()
    method = request_line[0].upper() if len(request_line) > 0 else "GET"
    path = request_line[1] if len(request_line) > 1 else "/"

    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    return method, path, headers, body


def _escape_md(text: str) -> str:
    for ch in ("_", "*", "[", "]", "`"):
        text = text.replace(ch, f"\\{ch}")
    return text


MAX_BODY_SIZE = 65536
MAX_HEADER_SIZE = 8192


async def _read_http_request(reader: asyncio.StreamReader) -> bytes:
    """Read a complete HTTP request including headers and content-length body."""
    buffer = bytearray()
    while b"\r\n\r\n" not in buffer and b"\n\n" not in buffer:
        if len(buffer) > MAX_HEADER_SIZE:
            return b""
        chunk = await reader.read(1024)
        if not chunk:
            break
        buffer.extend(chunk)

    if b"\r\n\r\n" in buffer:
        sep = b"\r\n\r\n"
    elif b"\n\n" in buffer:
        sep = b"\n\n"
    else:
        return bytes(buffer)

    header_bytes, body_bytes = buffer.split(sep, 1)
    headers_text = header_bytes.decode("utf-8", errors="replace")
    content_length = 0
    for line in headers_text.splitlines()[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            if k.strip().lower() == "content-length":
                try:
                    content_length = int(v.strip())
                except ValueError:
                    content_length = 0
                break

    if content_length > MAX_BODY_SIZE:
        return b""

    needed = content_length - len(body_bytes)
    if needed > 0:
        extra_body = await reader.readexactly(needed)
        body_bytes.extend(extra_body)

    return bytes(header_bytes + sep + body_bytes)


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, telegram_bot=None) -> None:
    try:
        try:
            data = await asyncio.wait_for(_read_http_request(reader), timeout=10.0)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            writer.close()
            await writer.wait_closed()
            return

        if not data:
            writer.close()
            await writer.wait_closed()
            return

        method, path, headers, body = _parse_http_request(data)
        parsed_url = urllib.parse.urlparse(path)
        path_only = parsed_url.path.rstrip("/")

        # Health check ping endpoint
        if method == "GET" and (path_only == "/health" or path_only == "/ping"):
            response = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\nConnection: close\r\n\r\n{\"status\":\"ok\"}"
            writer.write(response.encode("utf-8"))
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return

        # Webhook endpoint: POST /webhook or POST /webhook/<secret>
        if method != "POST" or (path_only != "/webhook" and not path_only.startswith("/webhook/")):
            response = "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            writer.write(response.encode("utf-8"))
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return

        # Validate secret: Fail-closed (require configured secret)
        expected_secret = (config.WEBHOOK_SECRET or "").strip()
        if not expected_secret:
            logger.warning("Rejected webhook attempt: WEBHOOK_SECRET is not configured in .env")
            err_body = "{\"error\":\"unauthorized\"}"
            response = f"HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nContent-Length: {len(err_body)}\r\nConnection: close\r\n\r\n{err_body}"
            writer.write(response.encode("utf-8"))
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return

        path_secret = path_only[len("/webhook/"):].strip("/") if path_only.startswith("/webhook/") else ""
        header_secret = headers.get("x-webhook-secret", "")
        valid_path = secrets.compare_digest(path_secret, expected_secret) if path_secret else False
        valid_header = secrets.compare_digest(header_secret, expected_secret) if header_secret else False

        if not (valid_path or valid_header):
            logger.warning("Rejected unauthorized webhook attempt (secret mismatch)")
            err_body = "{\"error\":\"unauthorized\"}"
            response = f"HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nContent-Length: {len(err_body)}\r\nConnection: close\r\n\r\n{err_body}"
            writer.write(response.encode("utf-8"))
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return

        # Parse body (JSON or raw text)
        ticker = ""
        action = ""
        price = ""
        message_text = ""

        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                ticker = str(payload.get("ticker") or payload.get("symbol") or "").upper()
                action = str(payload.get("action") or payload.get("signal") or "").upper()
                price = str(payload.get("price") or "")
                message_text = str(payload.get("message") or payload.get("text") or "")
                if not message_text and not ticker:
                    message_text = json.dumps(payload, indent=2)
            else:
                message_text = str(payload)
        except Exception:
            message_text = body.strip()

        # Build notification content
        title_tag = f" {ticker}" if ticker else ""
        act_tag = f" [{action}]" if action else ""
        title = f"🔔 TradingView Alert:{title_tag}{act_tag}".strip()

        push_msg = message_text or f"TradingView alert received for {ticker or 'market'}"
        if price:
            push_msg += f"\nPrice: ${price}"

        logger.info(f"Webhook alert received: {title} — {push_msg[:80]}")

        # Send push via ntfy
        await send_ntfy(
            title=title,
            message=push_msg,
            tags=["bell", "chart_with_upwards_trend"],
            priority="high"
        )

        # Forward to Telegram
        if telegram_bot and config.TELEGRAM_USER_ID:
            tg_lines = ["🔔 *External Webhook Alert*"]
            if ticker:
                tg_lines.append(f"*Ticker:* `{_escape_md(ticker)}`")
            if action:
                tg_lines.append(f"*Action:* *{_escape_md(action)}*")
            if price:
                tg_lines.append(f"*Price:* `${_escape_md(price)}`")
            if message_text:
                tg_lines.append(f"\n{_escape_md(message_text)}")

            try:
                await telegram_bot.send_message(
                    chat_id=config.TELEGRAM_USER_ID,
                    text="\n".join(tg_lines),
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.warning(f"Failed to forward webhook via Markdown, trying plain text: {e}")
                try:
                    plain_lines = ["🔔 External Webhook Alert"]
                    if ticker:
                        plain_lines.append(f"Ticker: {ticker}")
                    if action:
                        plain_lines.append(f"Action: {action}")
                    if price:
                        plain_lines.append(f"Price: ${price}")
                    if message_text:
                        plain_lines.append(f"\n{message_text}")
                    await telegram_bot.send_message(
                        chat_id=config.TELEGRAM_USER_ID,
                        text="\n".join(plain_lines),
                        parse_mode=None
                    )
                except Exception as inner_e:
                    logger.error(f"Failed plain text forward: {inner_e}")

        # Respond HTTP 200
        resp_body = "{\"status\":\"ok\",\"received\":true}"
        response = f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(resp_body)}\r\nConnection: close\r\n\r\n{resp_body}"
        writer.write(response.encode("utf-8"))
        await writer.drain()

    except Exception as e:
        logger.error(f"Error handling webhook request: {e}")
        try:
            err_body = "{\"error\":\"server_error\"}"
            response = f"HTTP/1.1 500 Internal Server Error\r\nContent-Length: {len(err_body)}\r\nConnection: close\r\n\r\n{err_body}"
            writer.write(response.encode("utf-8"))
            await writer.drain()
        except Exception:
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def run_webhook_server(telegram_bot=None) -> None:
    """Start the webhook listener loop."""
    global _server
    port = config.WEBHOOK_PORT or 8080
    try:
        _server = await asyncio.start_server(
            lambda r, w: _handle_client(r, w, telegram_bot=telegram_bot),
            host="0.0.0.0",
            port=port
        )
        logger.info(f"Webhook receiver listening on port {port} (POST /webhook/<SECRET>)")
        async with _server:
            await _server.serve_forever()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Webhook server error on port {port}: {e}")
    finally:
        if _server:
            _server.close()
            try:
                await _server.wait_closed()
            except Exception:
                pass
            _server = None
        logger.info("Webhook server stopped")


def stop_webhook_server() -> None:
    global _server
    if _server:
        _server.close()
