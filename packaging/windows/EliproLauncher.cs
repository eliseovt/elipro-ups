// GUI-subsystem launcher: no terminal flash for the panel or session startup.
// The console-subsystem Elipro.exe remains available for SCM and binary workers.
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class EliproLauncher
{
    [STAThread]
    private static int Main(string[] args)
    {
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
