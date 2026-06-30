"""Local HTTP helper for browser URL -> native MQTT TCP.

Browser posts JSON payloads to http://127.0.0.1:8765/publish.
This helper publishes those payloads to a normal MQTT TCP broker.
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import paho.mqtt.client as mqtt


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local browser-to-native-MQTT helper")
    parser.add_argument("--listen-host", default="127.0.0.1", help="HTTP helper listen host")
    parser.add_argument("--listen-port", type=int, default=8765, help="HTTP helper listen port")
    parser.add_argument("--mqtt-host", default="59.124.7.96", help="Native MQTT broker host/IP")
    parser.add_argument("--mqtt-port", type=int, default=1883, help="Native MQTT TCP port")
    parser.add_argument("--topic", default="height_cm", help="MQTT topic")
    parser.add_argument("--username", default="", help="Optional MQTT username")
    parser.add_argument("--password", default="", help="Optional MQTT password")
    return parser.parse_args()


def connect_mqtt(args: argparse.Namespace) -> mqtt.Client:
    callback_api = getattr(mqtt, "CallbackAPIVersion", None)
    if callback_api is not None:
        client = mqtt.Client(callback_api.VERSION2)
    else:
        client = mqtt.Client()
    if args.username:
        client.username_pw_set(args.username, args.password)
    client.connect(args.mqtt_host, args.mqtt_port, keepalive=60)
    client.loop_start()
    return client


def make_handler(client: mqtt.Client, topic: str) -> type[BaseHTTPRequestHandler]:
    class NativeMqttHandler(BaseHTTPRequestHandler):
        server_version = "NativeMqttHelper/1.0"

        def end_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            super().end_headers()

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.end_headers()

        def do_GET(self) -> None:
            if self.path not in ("/", "/health"):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true,"service":"native_mqtt_helper"}')

        def do_POST(self) -> None:
            if self.path != "/publish":
                self.send_error(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                payload: dict[str, Any] = json.loads(raw.decode("utf-8"))
                payload_text = json.dumps(payload, separators=(",", ":"))
                client.publish(topic, payload_text, qos=0, retain=False)
                print(f"{topic} {payload_text}")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')
            except Exception as err:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                message = json.dumps({"ok": False, "error": str(err)})
                self.wfile.write(message.encode("utf-8"))

        def log_message(self, format: str, *args: Any) -> None:
            return

    return NativeMqttHandler


def main() -> int:
    args = parse_args()
    print(f"Native MQTT TCP {args.mqtt_host}:{args.mqtt_port}, topic {args.topic}")
    print(f"HTTP helper http://{args.listen_host}:{args.listen_port}/publish")
    try:
        client = connect_mqtt(args)
    except Exception as err:
        print(f"MQTT connect failed: {err}", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((args.listen_host, args.listen_port), make_handler(client, args.topic))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping.")
    finally:
        server.server_close()
        client.loop_stop()
        client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
