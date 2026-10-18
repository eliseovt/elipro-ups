// GUI-subsystem launcher: no terminal flash for the panel or session startup.
// The console-subsystem Elipro.exe remains available for SCM and binary workers.
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows.Forms;

internal static class EliproLauncher
{
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetProcessDpiAwarenessContext(IntPtr dpiContext);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetProcessDPIAware();

    private static void ConfigureDpi()
    {
        try
        {
            // DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2, before any UI.
            if (SetProcessDpiAwarenessContext(new IntPtr(-4))) return;
            if (Marshal.GetLastWin32Error() == 5) return; // Already set by host/manifest.
        }
        catch (EntryPointNotFoundException) { }
        catch (DllNotFoundException) { return; }
        try { SetProcessDPIAware(); }
        catch (EntryPointNotFoundException) { }
        catch (DllNotFoundException) { }
    }

    [STAThread]
    private static int Main(string[] args)
    {
        ConfigureDpi();
        if (args.Length > 1 || (args.Length == 1 && args[0] != "--background"))
            return 2;

        string directory = AppDomain.CurrentDomain.BaseDirectory;
        string executable = Path.Combine(directory, "Elipro.exe");
        try
        {
            var command = new ProcessStartInfo
            {
                FileName = executable,
                Arguments = args.Length == 0 ? "agent --open-panel" : "agent",
                WorkingDirectory = directory,
                UseShellExecute = false,
                CreateNoWindow = true
            };
            Process child = Process.Start(command);
            if (child == null)
                throw new InvalidOperationException("No se pudo iniciar Elipro.");
            child.Dispose();
            return 0;
        }
        catch (Exception error)
        {
            MessageBox.Show("No se pudo abrir Elipro UPS.\n" + error.Message,
                "Elipro UPS", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
