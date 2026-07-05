# device runtime tool: keyboard.type
# 约定：对象 $toolArgs 已注入；把结果赋给 $result。由 powershell-runner 在设备上执行（Windows PowerShell 5.1 兼容）。
# 此文件是该工具的默认实现（真相源）；AI 可经 device_mcp.manage（action=upsert 同名工具）改写实例。
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace HS {
    public static class Kbd {
        [StructLayout(LayoutKind.Sequential)]
        public struct KEYBDINPUT { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public UIntPtr dwExtraInfo; }
        [StructLayout(LayoutKind.Sequential)]
        public struct MOUSEINPUT { public int dx; public int dy; public uint mouseData; public uint dwFlags; public uint time; public UIntPtr dwExtraInfo; }
        [StructLayout(LayoutKind.Explicit)]
        public struct UNION { [FieldOffset(0)] public MOUSEINPUT mi; [FieldOffset(0)] public KEYBDINPUT ki; }
        [StructLayout(LayoutKind.Sequential)]
        public struct INPUT { public uint type; public UNION U; }
        [DllImport("user32.dll", SetLastError = true)]
        public static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);
        const uint KEYEVENTF_KEYUP = 0x0002, KEYEVENTF_UNICODE = 0x0004;
        const ushort VK_RETURN = 0x0D, VK_TAB = 0x09;
        static void Tap(ushort vk) {
            INPUT[] inp = new INPUT[2];
            inp[0].type = 1; inp[0].U.ki.wVk = vk;
            inp[1].type = 1; inp[1].U.ki.wVk = vk; inp[1].U.ki.dwFlags = KEYEVENTF_KEYUP;
            SendInput(2, inp, Marshal.SizeOf(typeof(INPUT)));
        }
        // SendInput + KEYEVENTF_UNICODE：与焦点窗口键盘布局无关，可输入任意 Unicode 文本。
        public static void TypeText(string text, int delayMs) {
            foreach (char c in text) {
                if (c == '\r') continue;
                if (c == '\n') { Tap(VK_RETURN); }
                else if (c == '\t') { Tap(VK_TAB); }
                else {
                    INPUT[] inp = new INPUT[2];
                    inp[0].type = 1; inp[0].U.ki.wScan = c; inp[0].U.ki.dwFlags = KEYEVENTF_UNICODE;
                    inp[1].type = 1; inp[1].U.ki.wScan = c; inp[1].U.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP;
                    SendInput(2, inp, Marshal.SizeOf(typeof(INPUT)));
                }
                if (delayMs > 0) System.Threading.Thread.Sleep(delayMs);
            }
        }
    }
}
'@
[HS.Kbd]::TypeText([string]$toolArgs.text, 10)
$result = @{ ok = $true }
