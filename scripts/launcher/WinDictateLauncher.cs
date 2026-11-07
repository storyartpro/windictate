// WinDictate launcher — запуск и управление приложением «Диктовка».
//
// A small native Windows executable that starts, stops and restarts the WinDictate
// background app (pythonw + the faster-whisper sidecar) without a console window.
// It is what the user double-clicks, what the desktop/Start-menu shortcuts point to
// and what HKCU Run starts on login.
//
// Usage:
//   WinDictate.exe                 start; if already running ask "restart?"
//   WinDictate.exe --start         start only when not running
//   WinDictate.exe --restart       stop (whole tree) and start again
//   WinDictate.exe --stop          stop application and sidecar
//   WinDictate.exe --autostart     start hidden (used by HKCU Run)
//   WinDictate.exe --settings      open the settings window (running or not)
//   WinDictate.exe --open          open the dictation window (Ctrl+Alt+Space equivalent)
//   WinDictate.exe --status        exit 0 = running, 10 = stopped (no UI)
//   WinDictate.exe --version       show version
//   WinDictate.exe --help          show this help
//
// Build: powershell -ExecutionPolicy Bypass -File scripts\build-launcher.ps1
// No third-party libraries: System.Management (WMI) + Win32 via P/Invoke only.

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows.Forms;

[assembly: AssemblyTitle("WinDictate")]
[assembly: AssemblyDescription("Локальная диктовка по горячей клавише")]
[assembly: AssemblyProduct("WinDictate")]
[assembly: AssemblyCompany("")]
[assembly: AssemblyVersion("0.1.0.0")]
[assembly: AssemblyFileVersion("0.1.0.0")]

namespace WinDictateLauncher
{
    internal static class Program
    {
        private const string Caption = "Диктовка";
        private const int ExitRunning = 0;
        private const int ExitStopped = 10;

        // Portable mode: the exe sits in a self-contained folder with app\, runtime\,
        // data\ and portable.flag next to it. Installed mode keeps everything in
        // %LOCALAPPDATA%\WinDictate. One exe handles both.
        private static readonly string ExeDir = Path.GetDirectoryName(
            Assembly.GetExecutingAssembly().Location);
        private static readonly bool Portable =
            !string.IsNullOrEmpty(ExeDir) && File.Exists(Path.Combine(ExeDir, "portable.flag"));
        private static readonly string Root = Portable
            ? ExeDir
            : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "WinDictate");
        private static readonly string AppDir = Path.Combine(Root, "app");
        private static readonly string AppPy = Path.Combine(AppDir, "app.py");
        private static readonly string Pythonw = Portable
            ? Path.Combine(ExeDir, "runtime", "Scripts", "pythonw.exe")
            : Path.Combine(Root, ".venv", "Scripts", "pythonw.exe");
        private static readonly string DataDir = Portable ? Path.Combine(ExeDir, "data") : Root;
        private static readonly string SettingsPath = Path.Combine(DataDir, "settings.json");
        private static readonly string LogPath = Path.Combine(DataDir, "logs", "launcher.log");
        private static readonly string AppMarker = AppPy;
        private static readonly string SidecarMarker = Path.Combine(AppDir, "server", "stt_server.py");
        private static readonly string BootstrapCmd = Path.Combine(ExeDir ?? ".", "bootstrap.cmd");

        [STAThread]
        private static int Main(string[] args)
        {
            try
            {
                string command = args.Length > 0 ? args[0].Trim().ToLowerInvariant() : "";
                switch (command)
                {
                    case "--autostart":
                        return Autostart();
                    case "--start":
                        return StartOnly();
                    case "--restart":
                        return Restart();
                    case "--stop":
                        StopAll();
                        Log("stopped");
                        return 0;
                    case "--settings":
                        return OpenSettings();
                    case "--open":
                        return OpenDictation();
                    case "--status":
                        return IsAppRunning() ? ExitRunning : ExitStopped;
                    case "--version":
                        MessageBox.Show("Диктовка\nВерсия " + Version(), Caption,
                            MessageBoxButtons.OK, MessageBoxIcon.Information);
                        return 0;
                    case "--help":
                    case "/?":
                    case "-h":
                        ShowHelp();
                        return 0;
                    case "":
                        return DefaultAction();
                    default:
                        MessageBox.Show("Неизвестная команда: " + args[0] + "\n\n" + HelpText(), Caption,
                            MessageBoxButtons.OK, MessageBoxIcon.Warning);
                        return 2;
                }
            }
            catch (Exception ex)
            {
                Log("FATAL: " + ex);
                MessageBox.Show("Не удалось выполнить действие: " + ex.Message +
                                "\n\nПодробности: " + LogPath, Caption,
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }
        }

        // ------------------------------------------------------------------ actions

        private static int DefaultAction()
        {
            if (!IsAppRunning())
                return StartApp(false) ? 0 : 1;

            DialogResult answer = MessageBox.Show(
                "WinDictate уже запущен.\n\nПерезапустить приложение?",
                Caption, MessageBoxButtons.YesNo, MessageBoxIcon.Question,
                MessageBoxDefaultButton.Button2);
            if (answer == DialogResult.Yes)
                return Restart();
            return 0;
        }

        private static int Autostart()
        {
            if (IsAppRunning())
                return 0;
            return StartApp(false) ? 0 : 1;
        }

        private static int StartOnly()
        {
            if (IsAppRunning())
            {
                Log("--start: already running");
                return 0;
            }
            return StartApp(false) ? 0 : 1;
        }

        private static int Restart()
        {
            StopAll();
            System.Threading.Thread.Sleep(400);
            bool ok = StartApp(false);
            Log("restart: " + (ok ? "started" : "failed"));
            return ok ? 0 : 1;
        }

        private static int OpenSettings()
        {
            if (!IsAppRunning())
                return StartApp(true) ? 0 : 1;
            bool sent = SendHotkey(ReadSetting("settingsHotkey", "Ctrl+Alt+S"));
            if (!sent)
                MessageBox.Show(
                    "Приложение запущено, но послать горячую клавишу настроек не удалось.\n" +
                    "Откройте настройки через меню значка в трее.", Caption,
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return sent ? 0 : 1;
        }

        private static int OpenDictation()
        {
            if (!IsAppRunning())
                return StartApp(false) ? 0 : 1;
            if (!SendHotkey(ReadSetting("hotkey", "Ctrl+Alt+Space")))
            {
                MessageBox.Show(
                    "Не удалось послать горячую клавишу диктовки.\n" +
                    "Проверьте сочетание в настройках приложения.", Caption,
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return 1;
            }
            return 0;
        }

        // ------------------------------------------------------------------ process control

        private sealed class ProcInfo
        {
            public int Pid;
            public int ParentPid;
            public string CommandLine;
        }

        private static bool IsAppRunning()
        {
            return FindProcesses(AppMarker).Count > 0;
        }

        private static List<ProcInfo> FindProcesses(string marker)
        {
            var result = new List<ProcInfo>();
            try
            {
                using (var searcher = new System.Management.ManagementObjectSearcher(
                    "SELECT ProcessId, ParentProcessId, Name, CommandLine FROM Win32_Process " +
                    "WHERE Name='python.exe' OR Name='pythonw.exe'"))
                {
                    foreach (System.Management.ManagementObject item in searcher.Get())
                    {
                        string commandLine = item["CommandLine"] as string ?? "";
                        if (commandLine.IndexOf(marker, StringComparison.OrdinalIgnoreCase) < 0)
                            continue;
                        var info = new ProcInfo
                        {
                            Pid = Convert.ToInt32(item["ProcessId"]),
                            ParentPid = Convert.ToInt32(item["ParentProcessId"]),
                            CommandLine = commandLine,
                        };
                        result.Add(info);
                    }
                }
            }
            catch (Exception ex)
            {
                Log("WMI query failed: " + ex.Message);
            }
            return result;
        }

        private static void StopAll()
        {
            var appProcesses = FindProcesses(AppMarker);
            foreach (ProcInfo process in appProcesses)
                KillTree(process.Pid);
            if (appProcesses.Count > 0)
                System.Threading.Thread.Sleep(400);

            // Sidecar may outlive the app after a crash; the Job Object usually handles
            // this, but the launcher cleans up leftovers as a second line of defence.
            foreach (ProcInfo process in FindProcesses(SidecarMarker))
                KillTree(process.Pid);

            Log("stop: killed " + appProcesses.Count + " app process(es)");
        }

        private static void KillTree(int pid)
        {
            try
            {
                var psi = new ProcessStartInfo("taskkill", "/PID " + pid + " /T /F")
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                };
                using (Process kill = Process.Start(psi))
                {
                    if (kill != null && !kill.WaitForExit(15000))
                    {
                        try { kill.Kill(); } catch { }
                    }
                }
            }
            catch (Exception ex)
            {
                Log("taskkill " + pid + " failed: " + ex.Message);
            }
        }

        private static bool StartApp(bool showSettings)
        {
            if (Portable && !File.Exists(Pythonw))
                BootstrapRuntime();
            if (!File.Exists(Pythonw))
            {
                string hint = Portable
                    ? "Запустите bootstrap.cmd из папки портативной версии (один раз, нужен uv)."
                    : "Запустите scripts\\install.ps1 из папки win-dictate.";
                MessageBox.Show("Не найден Python-интерпретатор:\n" + Pythonw + "\n\n" + hint,
                    Caption, MessageBoxButtons.OK, MessageBoxIcon.Error);
                return false;
            }
            if (!File.Exists(AppPy))
            {
                MessageBox.Show("Не найден app.py:\n" + AppPy, Caption,
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
                return false;
            }
            var psi = new ProcessStartInfo
            {
                FileName = Pythonw,
                Arguments = "\"" + AppPy + "\"" + (showSettings ? " --settings" : " --autostart"),
                WorkingDirectory = AppDir,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            if (Portable)
            {
                Directory.CreateDirectory(Path.Combine(DataDir, "logs"));
                psi.EnvironmentVariables["WIN_DICTATE_PORTABLE"] = "1";
                psi.EnvironmentVariables["WIN_DICTATE_DATA"] = DataDir;
            }
            Process started = Process.Start(psi);
            Log("start: " + (started != null ? "ok pid=" + started.Id : "failed") +
                (showSettings ? " (settings)" : "") + (Portable ? " (portable)" : ""));
            return started != null;
        }

        // Первый запуск портативной версии: bootstrap.cmd создаёт runtime\ (uv venv +
        // faster-whisper/sounddevice/numpy/pystray/pillow, при наличии GPU — CUDA 12).
        private static void BootstrapRuntime()
        {
            if (!File.Exists(BootstrapCmd))
            {
                Log("bootstrap: not found " + BootstrapCmd);
                return;
            }
            try
            {
                Log("bootstrap: running " + BootstrapCmd);
                var psi = new ProcessStartInfo("cmd.exe", "/c \"" + BootstrapCmd + "\" --quiet")
                {
                    WorkingDirectory = ExeDir,
                    UseShellExecute = true,
                    WindowStyle = ProcessWindowStyle.Normal,
                };
                using (Process process = Process.Start(psi))
                {
                    if (process != null)
                        process.WaitForExit();
                }
                Log("bootstrap: exit " + (File.Exists(Pythonw) ? "ok" : "failed"));
            }
            catch (Exception ex)
            {
                Log("bootstrap failed: " + ex.Message);
            }
        }

        // ------------------------------------------------------------------ hotkeys

        private static bool SendHotkey(string spec)
        {
            try
            {
                List<byte> modifiers = new List<byte>();
                byte key = 0;
                foreach (string rawPart in spec.Split('+'))
                {
                    string part = rawPart.Trim();
                    if (part.Length == 0)
                        continue;
                    switch (part.ToLowerInvariant())
                    {
                        case "ctrl":
                        case "control":
                            modifiers.Add(0x11);
                            break;
                        case "alt":
                            modifiers.Add(0x12);
                            break;
                        case "shift":
                            modifiers.Add(0x10);
                            break;
                        case "win":
                            modifiers.Add(0x5B);
                            break;
                        default:
                            key = VirtualKey(part);
                            break;
                    }
                }
                if (key == 0)
                {
                    modifiers.Clear();
                    modifiers.Add(0x11);
                    modifiers.Add(0x12);
                    key = 0x20;
                }

                const uint KeyUp = 0x0002;
                foreach (byte modifier in modifiers)
                {
                    keybd_event(modifier, 0, 0, UIntPtr.Zero);
                    System.Threading.Thread.Sleep(20);
                }
                keybd_event(key, 0, 0, UIntPtr.Zero);
                System.Threading.Thread.Sleep(20);
                keybd_event(key, 0, KeyUp, UIntPtr.Zero);
                for (int i = modifiers.Count - 1; i >= 0; i--)
                {
                    keybd_event(modifiers[i], 0, KeyUp, UIntPtr.Zero);
                    System.Threading.Thread.Sleep(20);
                }
                Log("hotkey sent: " + spec);
                return true;
            }
            catch (Exception ex)
            {
                Log("hotkey send failed (" + spec + "): " + ex.Message);
                return false;
            }
        }

        private static byte VirtualKey(string name)
        {
            if (name.Length == 1)
            {
                char ch = char.ToUpperInvariant(name[0]);
                if (ch >= 'A' && ch <= 'Z') return (byte)ch;
                if (ch >= '0' && ch <= '9') return (byte)ch;
                switch (ch)
                {
                    case ',': return 0xBC;
                    case '.': return 0xBE;
                    case '/': return 0xBF;
                    case ';': return 0xBA;
                    case '\'': return 0xDE;
                    case '[': return 0xDB;
                    case ']': return 0xDD;
                    case '-': return 0xBD;
                    case '=': return 0xBB;
                    case '`': return 0xC0;
                }
            }
            switch (name.ToLowerInvariant())
            {
                case "space": return 0x20;
                case "enter": return 0x0D;
                case "esc":
                case "escape": return 0x1B;
                case "tab": return 0x09;
                case "backspace": return 0x08;
                case "insert": return 0x2D;
                case "delete": return 0x2E;
                case "home": return 0x24;
                case "end": return 0x23;
                case "pageup": return 0x21;
                case "pagedown": return 0x22;
                case "left": return 0x25;
                case "up": return 0x26;
                case "right": return 0x27;
                case "down": return 0x28;
            }
            if (name.StartsWith("f", StringComparison.OrdinalIgnoreCase))
            {
                int number;
                if (int.TryParse(name.Substring(1), out number) && number >= 1 && number <= 24)
                    return (byte)(0x70 + number - 1);
            }
            return 0;
        }

        // ------------------------------------------------------------------ settings / log

        private static string ReadSetting(string key, string fallback)
        {
            try
            {
                if (!File.Exists(SettingsPath))
                    return fallback;
                string json = File.ReadAllText(SettingsPath, Encoding.UTF8);
                Match match = Regex.Match(json, "\"" + Regex.Escape(key) + "\"\\s*:\\s*\"([^\"]+)\"");
                return match.Success ? match.Groups[1].Value : fallback;
            }
            catch
            {
                return fallback;
            }
        }

        private static string Version()
        {
            return Assembly.GetExecutingAssembly().GetName().Version.ToString(3);
        }

        private static void Log(string message)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(LogPath));
                string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " launcher: " + message +
                              Environment.NewLine;
                File.AppendAllText(LogPath, line, Encoding.UTF8);
            }
            catch
            {
                // Logging must never break the launcher.
            }
        }

        private static void ShowHelp()
        {
            MessageBox.Show(HelpText(), Caption, MessageBoxButtons.OK, MessageBoxIcon.Information);
        }

        private static string HelpText()
        {
            return "Диктовка — запуск и управление приложением.\n\n" +
                   "Без аргументов — запустить (или предложить перезапуск).\n" +
                   "--start      запустить, если ещё не запущено\n" +
                   "--restart    перезапустить приложение\n" +
                   "--stop       остановить приложение\n" +
                   "--settings   открыть настройки\n" +
                   "--open       открыть окно диктовки\n" +
                   "--status     код выхода 0 — работает, 10 — остановлено\n" +
                   "--version    версия\n\n" +
                   "Файлы: " + Root;
        }

        [System.Runtime.InteropServices.DllImport("user32.dll")]
        private static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);
    }
}
