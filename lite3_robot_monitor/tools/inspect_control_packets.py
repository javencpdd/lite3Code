"""Lite3 控制通道侦查工具（只读 + 受控发送）。

背景
----
监控工程读取的是 **120 → 本机 UDP 43897** 的状态报文；
运动控制走的是 **本机 → 120 UDP 43893**（120 侧 `jy_exe` 监听），
两者是**不同端口、不同报文格式**的两套协议，本工程当前只读、不写。

43893 是闭源 `jy_exe` 的私有协议，本工具**不猜测协议**，
而是提供两条务实能力：

1. 解析 tcpdump 抓到的 pcap，把控制报文 dump 出来，并对相邻帧做逐字节差分，
   快速定位"哪个偏移是速度 / 时间戳 / 序号"，供人工确认；
2. 在明确知晓某个报文含义后，以"必须先确认"的方式手工重放该报文，
   用于验证猜想（默认 dry-run，必须显式 --confirm 才会真正发出）。

典型流程见 README「控制通道侦查」章节。

用法
----
    # 在 103 上抓取自己发出的控制包（推荐，零侵入）
    sudo tcpdump -i any -nn udp dst port 43893 -w /tmp/ctrl.pcap -c 300
    #   （同时手动缓慢改变机器人速度，制造可比较的报文序列）

    # 1) 列出报文
    python tools/inspect_control_packets.py list /tmp/ctrl.pcap --port 43893

    # 2) 相邻帧差分，找出会变的字段
    python tools/inspect_control_packets.py diff /tmp/ctrl.pcap --port 43893

    # 3) 重放某一帧（先 dry-run 看内容，再加 --confirm 真发）
    python tools/inspect_control_packets.py send <hex> --ip 192.168.1.120 --port 43893
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

# pcap 文件头魔数（按小端读出的数值）
PCAP_MAGIC_LE = 0xA1B2C3D4      # 小端文件，微秒时间戳
PCAP_MAGIC_BE = 0xD4C3B2A1      # 大端文件，微秒时间戳
PCAP_MAGIC_LE_NS = 0xA1B23C4D   # 小端文件，纳秒时间戳
PCAP_MAGIC_BE_NS = 0x4D3CB2A1   # 大端文件，纳秒时间戳
PCAPNG_MAGIC = 0x0A0D0D0A       # pcapng，本工具不支持，给出转换提示

# 链路层类型
LINK_NULL = 0
LINK_ETHERNET = 1
LINK_LINUX_SLL = 113

# 一个 UDP 报文记录
@dataclass
class Packet:
    """从 pcap 中还原出的一个 UDP 报文。"""
    index: int
    ts: float
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    payload: bytes


def _iter_pcap_records(data: bytes, little_endian: bool, ns: bool) -> Iterator[tuple[float, bytes]]:
    """遍历 pcap 记录，产出 (时间戳秒, 原始链路层帧)。"""
    endian = '<' if little_endian else '>'
    offset = 24  # 跳过 24 字节文件头
    total = len(data)
    while offset + 16 <= total:
        ts_sec, ts_frac, incl_len, _orig_len = struct.unpack_from(endian + 'IIII', data, offset)
        offset += 16
        frame = data[offset:offset + incl_len]
        offset += incl_len
        ts = ts_sec + ts_frac / (1e9 if ns else 1e6)
        if len(frame) < incl_len:
            break
        yield ts, frame


def _strip_link_layer(frame: bytes, link_type: int) -> Optional[bytes]:
    """剥掉链路层头，返回 IP 报文。"""
    if link_type == LINK_ETHERNET:
        eth_type = struct.unpack_from('!H', frame, 12)[0]
        if eth_type != 0x0800:      # 非 IPv4（如 VLAN/ARP）跳过
            return None
        return frame[14:]
    if link_type == LINK_LINUX_SLL:
        return frame[16:]
    if link_type == LINK_NULL:
        return frame[4:]
    return None


def read_pcap(path: Path, port: Optional[int] = None,
              direction: str = 'any') -> list[Packet]:
    """读取 pcap 并按 UDP 端口过滤，返回报文列表。"""
    raw = path.read_bytes()
    if len(raw) < 24:
        raise ValueError("文件过小，不像合法的 pcap")

    magic = struct.unpack('<I', raw[:4])[0]
    if magic == PCAPNG_MAGIC:
        raise ValueError(
            "这是 pcapng 格式，本工具只支持经典 pcap。两种处理方式：\n"
            "  editcap -F pcap input.pcapng output.pcap\n"
            "  或重新抓包时不要加 --pcapng（tcpdump 默认写经典 pcap）"
        )

    if magic in (PCAP_MAGIC_LE, PCAP_MAGIC_LE_NS):
        little_endian, ns = True, magic == PCAP_MAGIC_LE_NS
    elif magic in (PCAP_MAGIC_BE, PCAP_MAGIC_BE_NS):
        little_endian, ns = False, magic == PCAP_MAGIC_BE_NS
    else:
        raise ValueError(f"无法识别的 pcap 魔数: 0x{magic:08X}")

    link_type = struct.unpack_from('<I' if little_endian else '>I', raw, 20)[0]
    packets: list[Packet] = []

    for idx, (ts, frame) in enumerate(_iter_pcap_records(raw, little_endian, ns)):
        ip = _strip_link_layer(frame, link_type)
        if ip is None or len(ip) < 20:
            continue
        version_ihl = ip[0]
        if version_ihl >> 4 != 4:            # 只处理 IPv4
            continue
        ihl = (version_ihl & 0x0F) * 4
        if ip[9] != 17:                      # protocol != UDP
            continue
        src_ip = '.'.join(str(b) for b in ip[12:16])
        dst_ip = '.'.join(str(b) for b in ip[16:20])
        udp = ip[ihl:]
        if len(udp) < 8:
            continue
        src_port, dst_port, length, _csum = struct.unpack_from('!HHHH', udp, 0)
        payload = udp[8:8 + max(0, length - 8)]

        if port is not None:
            if direction == 'dst' and dst_port != port:
                continue
            if direction == 'src' and src_port != port:
                continue
            if direction == 'any' and port not in (src_port, dst_port):
                continue

        packets.append(Packet(idx, ts, src_ip, dst_ip, src_port, dst_port, payload))

    return packets


def hex_dump(data: bytes, max_bytes: int = 96) -> list[str]:
    """生成十六进制转储行。"""
    lines: list[str] = []
    limit = min(len(data), max_bytes)
    for off in range(0, limit, 16):
        chunk = data[off:off + 16]
        hex_part = ' '.join(f'{b:02x}' for b in chunk).ljust(16 * 3 - 1)
        ascii_part = ''.join(chr(b) if 32 <= b <= 126 else '.' for b in chunk)
        lines.append(f"  {off:04x}: {hex_part}  {ascii_part}")
    if len(data) > max_bytes:
        lines.append(f"  ... 共 {len(data)} 字节，仅显示前 {max_bytes}")
    return lines


def decode_header(payload: bytes) -> Optional[tuple[int, int, int]]:
    """按状态报文同样的约定尝试解码头部：code / length / sequence。"""
    if len(payload) < 12:
        return None
    return struct.unpack('<3I', payload[:12])


def cmd_list(packets: Iterable[Packet], dump: bool = True) -> None:
    """列出报文概览，并按约定尝试解出头部字段。"""
    for p in packets:
        head = decode_header(p.payload)
        head_str = (
            f"code=0x{head[0]:04X} len={head[1]} seq={head[2]}" if head else "头部不足 12 字节"
        )
        print(f"[{p.index:04d}] {p.ts:.6f} {p.src_ip}:{p.src_port} -> {p.dst_ip}:{p.dst_port} "
              f"payload={len(p.payload)}B  {head_str}")
        if dump:
            for line in hex_dump(p.payload):
                print(line)
            print()


def _candidate_numbers(payload: bytes, off: int) -> Optional[dict]:
    """在指定偏移尝试多种数值格式解码，供人工判断。"""
    out: dict = {}
    if off + 4 <= len(payload):
        out['f32_le'] = struct.unpack_from('<f', payload, off)[0]
        out['f32_be'] = struct.unpack_from('>f', payload, off)[0]
        out['i32_le'] = struct.unpack_from('<i', payload, off)[0]
    if off + 8 <= len(payload):
        out['f64_le'] = struct.unpack_from('<d', payload, off)[0]
        out['i64_le'] = struct.unpack_from('<q', payload, off)[0]
    return out or None


def cmd_diff(packets: list[Packet], top: int = 20) -> None:
    """相邻帧差分：统计各偏移的变化频率，推断数据字段位置。"""
    if len(packets) < 2:
        print("报文不足 2 条，无法做差分。请让机器人处于运动状态后再抓包。")
        return

    ref_len = len(packets[0].payload)
    lengths = {len(p.payload) for p in packets}
    print(f"报文条数 {len(packets)}，载荷长度集合 {sorted(lengths)}")

    if len(lengths) > 1:
        print("注意：载荷长度不一致，可能混入了不同类型的报文，建议先用 list 检查。")

    change_count: dict[int, int] = {}
    previous = packets[0].payload
    for p in packets[1:]:
        cur = p.payload
        limit = min(len(cur), len(previous))
        for off in range(limit):
            if cur[off] != previous[off]:
                change_count[off] = change_count.get(off, 0) + 1
        previous = cur

    if not change_count:
        print("所有相邻帧完全相同：这段抓包里没有任何字段在变，")
        print("通常是机器人静止且位置在全零点，或抓到的是同一份缓存重发。")
        return

    ranked = sorted(change_count.items(), key=lambda kv: (-kv[1], kv[0]))
    comparisons = len(packets) - 1
    print(f"\n相邻帧共 {comparisons} 次比较，其中有 {len(ranked)} 个字节偏移发生过变化。")

    # 把连续偏移聚合成"字段区间"，区间按变化频率从高到低排序
    changed_offsets = sorted(change_count)
    groups: list[list[int]] = []
    for off in changed_offsets:
        if groups and groups[-1][-1] + 1 == off:
            groups[-1].append(off)
        else:
            groups.append([off])
    groups.sort(key=lambda g: (-max(change_count[o] for o in g), g[0]))

    last = packets[-1].payload
    print(f"\n最可能承载数据的 {min(top, len(groups))} 个偏移区间：")
    print("  区间          帧数占比   候选解码（取最后一帧）")

    for group in groups[:top]:
        start, end = group[0], group[-1]
        ratio = max(change_count[o] for o in group) / comparisons
        nums = _candidate_numbers(last, start) or {}
        num_str = '  '.join(f"{k}={v:+.4f}" for k, v in nums.items())
        span = f"{start:04d}" if start == end else f"{start:04d}-{end:04d}"
        print(f"  {span:<13s} {ratio:7.1%}   {num_str}")

    print("\n判读提示：")
    print("  · 每帧必变且单调递增 → 时间戳或序号（通常 4/8 字节，位于头部）")
    print("  · 随操作有界变化、取值在 ±3 以内 → 极可能是速度 / 角速度（float32 / float64）")
    print("  · 长期不变 → 常量版本号、保留位、全零填充")
    print("  · 请把以上结果与实际操作对照确认，不要凭猜测下发指令")


def cmd_send(hex_str: str, ip: str, port: int, confirm: bool) -> None:
    """手工发送一个十六进制报文；默认 dry-run。"""
    cleaned = ''.join(hex_str.split()).replace('0x', '').replace(',', '')
    try:
        payload = bytes.fromhex(cleaned)
    except ValueError as exc:
        print(f"十六进制解析失败: {exc}")
        sys.exit(2)

    head = decode_header(payload)
    head_str = f"code=0x{head[0]:04X} len={head[1]} seq={head[2]}" if head else "头部不足 12 字节"
    print(f"目标 {ip}:{port}，长度 {len(payload)} 字节，{head_str}")
    for line in hex_dump(payload):
        print(line)

    if not confirm:
        print("\n[dry-run] 未发送。确认无误后追加 --confirm 才会真正发到 120。")
        print("警告：43893 直接接入闭源运动控制，错误的报文可能让机器人立即动作。")
        return

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(payload, (ip, port))
        print(f"\n已发送 {len(payload)} 字节到 {ip}:{port}")
        print("请密切注意机器人动作，随时准备用遥控器/急停接管。")
    finally:
        sock.close()


def build_parser() -> argparse.ArgumentParser:
    """构建命令行解析器。"""
    parser = argparse.ArgumentParser(description="Lite3 控制通道侦查工具")
    sub = parser.add_subparsers(dest='command', required=True)

    for name in ('list', 'diff'):
        sp = sub.add_parser(name, help=f"{'列出' if name == 'list' else '差分'}控制报文")
        sp.add_argument('pcap', type=Path, help="tcpdump 抓到的 pcap 文件")
        sp.add_argument('--port', type=int, default=43893, help="目标 UDP 端口，默认 43893")
        sp.add_argument('--dir', choices=('src', 'dst', 'any'), default='any',
                        help="按源端口/目的端口/任意方向过滤")
        if name == 'list':
            sp.add_argument('--no-dump', action='store_true', help="只打印概览，不做十六进制转储")
        else:
            sp.add_argument('--top', type=int, default=20, help="输出前 N 个变化最频繁的偏移")

    sp = sub.add_parser('send', help="手工发送一个十六进制报文")
    sp.add_argument('hex', help="报文十六进制串，例如 '01090000...' ")
    sp.add_argument('--ip', default='192.168.1.120', help="目标 IP，默认 192.168.1.120")
    sp.add_argument('--port', type=int, default=43893, help="目标端口，默认 43893")
    sp.add_argument('--confirm', action='store_true', help="真正发送，否则只 dry-run")

    return parser


def main() -> None:
    """命令行入口。"""
    args = build_parser().parse_args()

    if args.command in ('list', 'diff'):
        if not args.pcap.is_file():
            print(f"文件不存在: {args.pcap}")
            sys.exit(1)
        try:
            packets = read_pcap(args.pcap, args.port, args.dir)
        except ValueError as exc:
            print(f"读取 pcap 失败: {exc}")
            sys.exit(1)
        if not packets:
            print(f"没有匹配 port={args.port} dir={args.dir} 的 UDP 报文。")
            print("检查命令：sudo tcpdump -i any -nn udp dst port 43893 -w ctrl.pcap")
            return
        if args.command == 'list':
            cmd_list(packets, dump=not args.no_dump)
        else:
            cmd_diff(packets, args.top)
    else:
        cmd_send(args.hex, args.ip, args.port, args.confirm)


if __name__ == '__main__':
    main()
