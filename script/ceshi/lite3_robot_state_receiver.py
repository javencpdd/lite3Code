# -*- coding: utf-8 -*-
"""
Lite3 机器人 UDP 状态接收与可视化工具
-------------------------------------------------
主要功能：
1. 在本机 UDP 43897 端口监听 Lite3 状态数据；
2. 根据消息码 0x0901 / 0x0902 / 0x0903 解析机器人状态、关节角度和关节角速度；
3. 使用后台线程持续接收 UDP 数据，避免阻塞 Tkinter GUI；
4. 通过 Tkinter 界面显示解析后的状态或原始十六进制数据。

注意：本脚本只负责“接收和显示状态”，不会向机器人发送运动控制指令。
"""

import socket
import struct
import tkinter as tk
from tkinter import scrolledtext
import threading
import queue
import re

class RobotStateReceiver:
    """Lite3 状态接收器：负责 UDP 接收、协议解析和 GUI 显示。"""
    def __init__(self, local_port=43897):
        # 保存本地监听端口。Lite3 的状态数据需要发送到这个端口。
        self.local_port = local_port

        # 创建 IPv4 UDP Socket。
        # SOCK_DGRAM 表示 UDP；UDP 无连接、开销低，适合机器人高频状态数据传输。
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # 0.0.0.0 表示监听本机所有网卡上的该 UDP 端口，
            # 因此无论 Lite3 数据从有线网卡还是无线网卡进入，都可以被接收。
            self.sock.bind(("0.0.0.0", local_port))
            print(f"已绑定端口 {local_port}")
        except Exception as e:
            print(f"绑定端口失败: {e}")
            exit(1)
        # 设置 0.1 秒接收超时，避免 recvfrom() 永久阻塞，
        # 这样程序关闭时后台线程能够及时退出。
        self.sock.settimeout(0.1)

        # 线程安全队列：后台接收线程将 UDP 数据放入队列，
        # GUI 主线程再从队列读取并解析，避免直接跨线程修改 Tkinter 控件。
        self.recv_queue = queue.Queue()
        # 后台接收线程运行标志。
        self.running = True

        # UDP 接收必须放到后台线程，否则 recvfrom() 会阻塞 GUI 主循环。
        # daemon=True 表示主程序退出时该线程不会阻止进程结束。
        self.recv_thread = threading.Thread(target=self.receive_loop, daemon=True)
        self.recv_thread.start()

        # 状态映射字典
        self.basic_state_map = {
            1: "趴下状态", 4: "准备起立", 5: "正在起立", 6: "力控状态",
            7: "正在趴下", 8: "失控保护", 9: "姿态调整", 11: "执行翻身",
            16: "AI状态", 17: "回零状态", 18: "执行后空翻", 20: "执行打招呼",
            98: "未初始化(关节未回零)"
        }
        self.gait_state_map = {
            0: "平地低速", 2: "通用越障", 4: "平地中速", 5: "平地高速",
            6: "抓地越障", 12: "太空步", 13: "高踏步"
        }
        self.policy_state_map = {
            0: "AI基础步态",
            16: "AI跳跃步态",
            18: "AI站立步态",
            20: "AI极速步态"
        }
        self.motion_state_map = {
            0: "无动作", 1: "踏步", 2: "扭身体", 4: "扭身跳", 11: "向前跳"
        }

        # ---------------- GUI 初始化 ----------------
        # Tkinter 主线程负责窗口、按钮以及状态文本显示。
        self.root = tk.Tk()
        self.root.title("机器人状态接收器 (v1.0.8)")
        self.root.geometry("1100x750")

        btn_frame = tk.Frame(self.root)
        btn_frame.pack(pady=10)

        tk.Button(btn_frame, text="获取机器人状态 (0x0901)", width=18,
                  command=self.get_robot_state).pack(side=tk.LEFT, padx=3)
        tk.Button(btn_frame, text="获取关节角度 (0x0902)", width=18,
                  command=self.get_joint_angles).pack(side=tk.LEFT, padx=3)
        tk.Button(btn_frame, text="获取关节角速度 (0x0903)", width=18,
                  command=self.get_joint_velocities).pack(side=tk.LEFT, padx=3)
        tk.Button(btn_frame, text="显示最新原始数据", width=15,
                  command=self.show_raw_data).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="原始0x0901", width=10,
                  command=lambda: self.show_raw_data(0x0901)).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_frame, text="原始0x0902", width=10,
                  command=lambda: self.show_raw_data(0x0902)).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_frame, text="原始0x0903", width=10,
                  command=lambda: self.show_raw_data(0x0903)).pack(side=tk.LEFT, padx=2)

        self.result_text = scrolledtext.ScrolledText(self.root, wrap=tk.WORD,
                                                      font=("Consolas", 10))
        self.result_text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.result_text.tag_config("chinese", foreground="black")
        self.result_text.tag_config("english", foreground="blue", font=("Consolas", 10, "italic"))
        self.result_text.tag_config("value", foreground="black")
        self.result_text.tag_config("invalid", foreground="gray", font=("Consolas", 9))

        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def receive_loop(self):
        """后台线程：持续接收 Lite3 发来的 UDP 数据包并写入队列。"""
        while self.running:
            try:
                # recvfrom() 返回：
                # data：收到的二进制 UDP 数据；addr：(源 IP, 源端口)。
                data, addr = self.sock.recvfrom(2048)

                # 不在接收线程中直接解析和操作 GUI，只负责缓存数据。
                self.recv_queue.put((data, addr))
            except socket.timeout:
                continue
            except socket.error as e:
                if not self.running:
                    break
                print(f"接收错误: {e}")
                break
            except Exception as e:
                print(f"接收错误: {e}")
                break

    def parse_robot_state(self, data):
        """解析消息码 0x0901：机器人综合状态。"""
        # 数据包前 12 字节为通用消息头，后续 208 字节为机器人状态负载。
        if len(data) < 12 + 208:
            return f"数据长度不足: 期望 220 字节，实际 {len(data)} 字节"
        # struct 格式说明：
        # '<'   ：小端字节序；
        # i / I ：32 位有符号 / 无符号整数；
        # d     ：64 位 double；
        # B     ：8 位无符号整数；
        # 3x    ：跳过 3 个填充字节。
        # 该格式必须与 Lite3 发送端 C/C++ 结构体的字段顺序、字节对齐保持一致。
        fmt = '<iiiI' + 'd' * 18 + 'I B 3x I i d i 4B 2d'
        try:
            # 从偏移 12 字节开始解析，即跳过数据包消息头。
            values = struct.unpack_from(fmt, data, 12)
        except struct.error as e:
            return f"解析失败: {e}"

        it = iter(values)
        basic_state = next(it)
        gait_state = next(it)
        policy_state = next(it)
        _ = next(it)  # 保留填充
        # IMU 姿态：Roll、Pitch、Yaw。
        rpy = [next(it) for _ in range(3)]
        rpy_vel = [next(it) for _ in range(3)]
        xyz_acc = [next(it) for _ in range(3)]
        # 世界坐标系中的二维位置/航向，以及世界系和机体系速度。
        # 这里三个分量通常可理解为 x、y、yaw 或 vx、vy、yaw_rate。
        pos_world = [next(it) for _ in range(3)]
        vel_world = [next(it) for _ in range(3)]
        vel_body = [next(it) for _ in range(3)]
        touch_down = next(it)
        is_charging_byte = next(it)
        # 跳过3字节填充
        error_state = next(it)
        motion_state = next(it)
        battery = next(it)
        task_state = next(it)
        flag_bytes = [next(it) for _ in range(4)]
        ultrasound = [next(it) for _ in range(2)]

        # 将协议中的 0/1 字节转换成 Python bool，便于后续显示和逻辑判断。
        is_robot_need_move = bool(flag_bytes[0])
        zero_position_flag = bool(flag_bytes[1])
        is_after_first_start = bool(flag_bytes[2])
        is_voice_ctrl_enable = bool(flag_bytes[3])
        is_charging = bool(is_charging_byte & 0xFF)

        # 映射
        basic_state_str = self.basic_state_map.get(basic_state, f"未知({basic_state})")
        gait_state_str = self.gait_state_map.get(gait_state, f"未知({gait_state})")
        policy_state_str = self.policy_state_map.get(policy_state, f"未知({policy_state})")
        motion_state_str = self.motion_state_map.get(motion_state, f"未知({motion_state})")

        # 电池电量处理
        if battery <= 1.0:
            battery_percent = battery * 100
        else:
            battery_percent = battery

        lines = []
        lines.append(f"【机器人基本状态 [EN](robot_basic_state)[/EN]】 {basic_state_str}")
        lines.append(f"【当前步态 [EN](robot_gait_state)[/EN]】 {gait_state_str}")
        lines.append(f"【AI步态 [EN](robot_policy_state)[/EN]】 {policy_state_str}")
        lines.append(f"【IMU 角度 [EN](rpy)[/EN]】")
        lines.append(f"  Roll  [EN](roll)[/EN] : {rpy[0]:.3f}°")
        lines.append(f"  Pitch [EN](pitch)[/EN]: {rpy[1]:.3f}°")
        lines.append(f"  Yaw   [EN](yaw)[/EN]  : {rpy[2]:.3f}°")
        lines.append(f"【IMU 角速度 [EN](rpy_vel)[/EN]】")
        lines.append(f"  Roll_vel  [EN](roll_vel)[/EN] : {rpy_vel[0]:.3f} rad/s")
        lines.append(f"  Pitch_vel [EN](pitch_vel)[/EN]: {rpy_vel[1]:.3f} rad/s")
        lines.append(f"  Yaw_vel   [EN](yaw_vel)[/EN]  : {rpy_vel[2]:.3f} rad/s")
        lines.append(f"【IMU 加速度 [EN](xyz_acc)[/EN]】")
        lines.append(f"  Xacc [EN](x_acc)[/EN] : {xyz_acc[0]:.3f} m/s²")
        lines.append(f"  Yacc [EN](y_acc)[/EN] : {xyz_acc[1]:.3f} m/s²")
        lines.append(f"  Zacc [EN](z_acc)[/EN] : {xyz_acc[2]:.3f} m/s²")
        lines.append(f"【世界坐标系位置 [EN](pos_world)[/EN]】")
        lines.append(f"  X   [EN](x)[/EN]  : {pos_world[0]:.3f} m")
        lines.append(f"  Y   [EN](y)[/EN]  : {pos_world[1]:.3f} m")
        lines.append(f"  Yaw [EN](yaw)[/EN]: {pos_world[2]:.3f} rad")
        lines.append(f"【世界坐标系速度 [EN](vel_world)[/EN]】")
        lines.append(f"  X_vel   [EN](x_vel)[/EN]  : {vel_world[0]:.3f} m/s")
        lines.append(f"  Y_vel   [EN](y_vel)[/EN]  : {vel_world[1]:.3f} m/s")
        lines.append(f"  Yaw_vel [EN](yaw_vel)[/EN]: {vel_world[2]:.3f} rad/s")
        lines.append(f"【机体坐标系速度 [EN](vel_body)[/EN]】")
        lines.append(f"  X_vel   [EN](x_vel)[/EN]  : {vel_body[0]:.3f} m/s")
        lines.append(f"  Y_vel   [EN](y_vel)[/EN]  : {vel_body[1]:.3f} m/s")
        lines.append(f"  Yaw_vel [EN](yaw_vel)[/EN]: {vel_body[2]:.3f} rad/s")
        lines.append(f"【touch_down_and_stair_trot [EN](touch_down_and_stair_trot)[/EN]】 {touch_down} （无效数据）")
        lines.append(f"【is_charging [EN](is_charging)[/EN]】 {is_charging_byte} （无效数据）")
        lines.append(f"【error_state [EN](error_state)[/EN]】 {error_state} （无效数据）")
        lines.append(f"【动作状态 [EN](robot_motion_state)[/EN]】 {motion_state_str}")
        lines.append(f"【电池电量 [EN](battery_level)[/EN]】 {battery_percent:.1f}%")
        lines.append(f"【task_state [EN](task_state)[/EN]】 {task_state} （无效数据）")
        lines.append(f"【外力平衡 [EN](is_robot_need_move)[/EN]】 {'需调整' if is_robot_need_move else '稳定'}")
        zero_flag_desc = "已完成回零" if zero_position_flag else "未完成回零或已退出回零状态"
        lines.append(f"【回零标志 [EN](zero_position_flag)[/EN]】 {zero_flag_desc}")
        lines.append(f"【首次开启标志 [EN](is_after_first_start)[/EN]】 {'是' if is_after_first_start else '否'}")
        lines.append(f"【语音控制功能 [EN](is_voice_ctrl_enable)[/EN]】 {'开启' if is_voice_ctrl_enable else '关闭'}")
        lines.append(f"【超声波 [EN](ultrasound)[/EN]】")
        lines.append(f"  前方 [EN](forward_distance)[/EN]: {ultrasound[0]:.3f} m (量程 0.28-4.50)")
        lines.append(f"  后方 [EN](backward_distance)[/EN]: {ultrasound[1]:.3f} m")

        return "\n".join(lines)

    def parse_joint_angles(self, data):
        """解析消息码 0x0902：12 个电机关节角度，单位 rad。"""
        if len(data) < 12 + 96:
            return f"数据长度不足: 期望 108 字节，实际 {len(data)} 字节"
        # Lite3 四足机器人共有 12 个关节，这里按 12 个 double 顺序解析。
        fmt = '<' + 'd'*12
        try:
            angles = struct.unpack_from(fmt, data, 12)
        except struct.error as e:
            return f"解析失败: {e}"
        lines = ["【关节角度 [EN](joint_angle)[/EN]】"]
        for i, val in enumerate(angles):
            lines.append(f"  joint_{i:2d}: {val:.6f} rad")
        return "\n".join(lines)

    def parse_joint_velocities(self, data):
        """解析消息码 0x0903：12 个电机关节角速度，单位 rad/s。"""
        if len(data) < 12 + 96:
            return f"数据长度不足: 期望 108 字节，实际 {len(data)} 字节"
        # 与关节角度相同，共解析 12 个 double；这里表示角速度 rad/s。
        fmt = '<' + 'd'*12
        try:
            vels = struct.unpack_from(fmt, data, 12)
        except struct.error as e:
            return f"解析失败: {e}"
        lines = ["【关节角速度 [EN](joint_vel)[/EN]】  注意：静止时可能存在噪声/漂移"]
        for i, val in enumerate(vels):
            lines.append(f"  joint_{i:2d}: {val:.6f} rad/s")
        return "\n".join(lines)

    def get_latest_packet(self, target_code):
        """从接收队列中取出指定消息码的最新一个数据包。"""
        # 注意：这里会把当前队列中的数据全部取出，因此它不是“主动向机器人查询”。
        packets = []
        while not self.recv_queue.empty():
            try:
                data, addr = self.recv_queue.get_nowait()
                if len(data) >= 12:
                    # 通用消息头前 12 字节按 3 个 uint32 解析。
                    # 第一个字段 code 用来区分 0x0901/0x0902/0x0903 等消息类型。
                    code, _, _ = struct.unpack('<3I', data[:12])
                    packets.append((code, data, addr))
            except queue.Empty:
                break
        filtered = [(c, d, a) for c, d, a in packets if c == target_code]
        if filtered:
            code, data, addr = filtered[-1]
            result = self.parse_by_code(code, data)
            return f"来自 {addr}\n数据包长度: {len(data)} 字节\n\n" + result
        else:
            if packets:
                last_code, last_data, last_addr = packets[-1]
                return f"未找到 0x{target_code:X} 数据，最后收到的是 0x{last_code:X}，长度 {len(last_data)} 字节，来自 {last_addr}"
            else:
                return "队列为空，尚未收到任何数据包。"

    def parse_by_code(self, code, data):
        """根据消息码将数据分发给对应的解析函数。"""
        if code == 0x0901:
            return self.parse_robot_state(data)
        elif code == 0x0902:
            return self.parse_joint_angles(data)
        elif code == 0x0903:
            return self.parse_joint_velocities(data)
        else:
            return f"未知指令码 0x{code:X}，数据长度 {len(data)}"

    def show_raw_data(self, target_code=None):
        """以十六进制 + ASCII 的形式显示最新原始 UDP 数据，便于协议调试。"""
        packets = []
        while not self.recv_queue.empty():
            try:
                data, addr = self.recv_queue.get_nowait()
                packets.append((data, addr))
            except queue.Empty:
                break
        if not packets:
            self.result_text.delete(1.0, tk.END)
            self.result_text.insert(tk.END, "队列为空，尚未收到任何数据包。")
            return

        if target_code is not None:
            filtered = []
            for data, addr in packets:
                if len(data) >= 12:
                    code, _, _ = struct.unpack('<3I', data[:12])
                    if code == target_code:
                        filtered.append((data, addr))
            if filtered:
                data, addr = filtered[-1]
                title = f"最新 0x{target_code:X} 原始数据"
            else:
                data, addr = packets[-1]
                title = f"未找到 0x{target_code:X} 数据，显示最新任意包"
        else:
            data, addr = packets[-1]
            title = "最新原始数据"

        # 为避免 GUI 一次显示过多内容，最多展示前 400 字节。
        max_bytes = min(len(data), 400)
        hex_lines = []
        for i in range(0, max_bytes, 16):
            chunk = data[i:i+16]
            hex_part = ' '.join(f'{b:02x}' for b in chunk)
            hex_part = hex_part.ljust(16*3 - 1)
            ascii_part = ''.join(chr(b) if 32 <= b <= 126 else '.' for b in chunk)
            hex_lines.append(f"{i:04x}: {hex_part}  {ascii_part}")
        display = "\n".join(hex_lines)
        if len(data) > 400:
            display += f"\n... (共 {len(data)} 字节，仅显示前 400)"

        self.result_text.delete(1.0, tk.END)
        self.result_text.insert(tk.END, f"=== {title} ===\n")
        self.result_text.insert(tk.END, f"来自 {addr}\n长度 {len(data)} 字节\n\n")
        self.result_text.insert(tk.END, display)

    def show_result(self, title, content):
        """将解析后的文本写入 Tkinter 文本框，并对 [EN] 标签内容单独着色。"""
        self.result_text.delete(1.0, tk.END)
        self.result_text.insert(tk.END, f"=== {title} ===\n\n", "chinese")

        for line in content.split('\n'):
            if not line:
                continue
            parts = []
            last_end = 0
            for match in re.finditer(r'\[EN\](.*?)\[/EN\]', line):
                start, end = match.span()
                if last_end < start:
                    parts.append((line[last_end:start], "chinese"))
                parts.append((match.group(1), "english"))
                last_end = end
            if last_end < len(line):
                parts.append((line[last_end:], "chinese"))

            for text, tag in parts:
                self.result_text.insert(tk.END, text, tag)
            self.result_text.insert(tk.END, "\n")

    # 以下三个按钮回调只是读取“已经收到的最新数据”，不会向 Lite3 主动发送查询命令。
    def get_robot_state(self):
        result = self.get_latest_packet(0x0901)
        self.show_result("机器人状态 (v1.0.8)", result)

    def get_joint_angles(self):
        result = self.get_latest_packet(0x0902)
        self.show_result("关节角度", result)

    def get_joint_velocities(self):
        result = self.get_latest_packet(0x0903)
        self.show_result("关节角速度", result)

    def on_closing(self):
        """窗口关闭时停止接收线程、关闭 UDP Socket，再销毁 GUI。"""
        self.running = False
        self.sock.close()
        if self.recv_thread.is_alive():
            self.recv_thread.join(timeout=1)
        self.root.destroy()

    def run(self):
        # 启动 Tkinter GUI 事件循环；此调用会一直运行到窗口关闭。
        self.root.mainloop()

if __name__ == "__main__":
    # 程序入口：监听本机 UDP 43897 端口。
    # 若 Lite3 发送端使用的是其他目标端口，需要同步修改这里。
    receiver = RobotStateReceiver(local_port=43897)
    receiver.run()