"""Foxglove 通路自检：手搓最小 WebSocket 客户端直连 rosbridge 订阅图像话题。

103 上没装 websocket-client / websockets，这里按 RFC6455 手写握手与帧收发（客户端帧必须掩码）。

用法：
  1) 起 rosbridge： source /opt/ros/foxy/setup.bash
                    ros2 run rosbridge_server rosbridge_websocket --port 9090
  2) cd /home/test/yolo8/src/test && python3 -u verify_foxglove_path.py
"""
import base64
import json
import os
import socket
import struct
import sys
import time

HOST = os.environ.get("ROSBRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ROSBRIDGE_PORT", "9090"))
TOPIC = os.environ.get("YOLO8_IMAGE_TOPIC", "/yolo/image_annotated")
SECONDS = float(os.environ.get("VERIFY_SECONDS", "10"))


def handshake(sock):
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        "GET / HTTP/1.1\r\n"
        "Host: " + HOST + ":" + str(PORT) + "\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: " + key + "\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("握手失败：连接被关闭")
        buf += chunk
    head = buf.split(b"\r\n\r\n", 1)[0].decode(errors="ignore")
    if "101" not in head.split("\r\n")[0]:
        raise RuntimeError("握手失败：\n" + head)
    return buf.split(b"\r\n\r\n", 1)[1]


def send_text(sock, text):
    payload = text.encode()
    header = bytearray([0x81])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    mask = os.urandom(4)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + mask + masked)


def read_frames(sock, rest, deadline, stats):
    buf = rest
    sock.settimeout(0.5)
    while time.time() < deadline:
        try:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        except socket.timeout:
            pass
        while True:
            if len(buf) < 2:
                break
            opcode = buf[0] & 0x0F
            masked = buf[1] & 0x80
            length = buf[1] & 0x7F
            offset = 2
            if length == 126:
                if len(buf) < 4:
                    break
                length = struct.unpack(">H", buf[2:4])[0]
                offset = 4
            elif length == 127:
                if len(buf) < 10:
                    break
                length = struct.unpack(">Q", buf[2:10])[0]
                offset = 10
            if masked:
                mask_key = buf[offset:offset + 4]
                offset += 4
            if len(buf) < offset + length:
                break
            data = buf[offset:offset + length]
            if masked:
                data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
            buf = buf[offset + length:]
            if opcode == 0x1:  # text
                stats["frames"] += 1
                try:
                    obj = json.loads(data.decode("utf-8", "ignore"))
                except ValueError:
                    continue
                if obj.get("op") == "publish" and obj.get("topic") == TOPIC:
                    msg = obj.get("msg", {})
                    payload = msg.get("data", "")
                    stats["msgs"] += 1
                    stats["bytes"] += len(payload)
                    if stats["first"] is None:
                        stats["first"] = time.time()
                        stats["fmt"] = msg.get("format")
                        stats["fid"] = msg.get("header", {}).get("frame_id")


def main():
    sock = socket.create_connection((HOST, PORT), timeout=5)
    rest = handshake(sock)
    print("ws handshake OK -> ws://%s:%d" % (HOST, PORT))

    send_text(sock, json.dumps({
        "op": "subscribe",
        "topic": TOPIC,
        "type": "sensor_msgs/CompressedImage",
        "throttle_rate": 0,
        "queue_length": 1,
        "compression": "none",
    }))
    print("subscribed:", TOPIC)

    stats = {"frames": 0, "msgs": 0, "bytes": 0, "first": None, "fmt": None, "fid": None}
    deadline = time.time() + SECONDS
    read_frames(sock, rest, deadline, stats)
    sock.close()

    n = stats["msgs"]
    print("ws frames : %d" % stats["frames"])
    print("messages  : %d in %.0fs" % (n, SECONDS))
    if n:
        print("rate      : %.2f Hz" % (n / SECONDS))
        print("format    : %s, frame_id=%s" % (stats["fmt"], stats["fid"]))
        print("avg size  : %.1f KB" % (stats["bytes"] / n / 1024.0))
    print("RESULT    : %s" % ("PASS" if n > 0 else "FAIL"))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("EXCEPTION: " + str(exc))
        sys.exit(1)
