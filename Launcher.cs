using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;
using Microsoft.Win32;

static class Launcher
{
    [STAThread] static void Main()
    {
        Application.EnableVisualStyles();
        string folder=AppDomain.CurrentDomain.BaseDirectory;
        string app=Path.Combine(folder,"app.py");
        if(!File.Exists(app)) { MessageBox.Show("app.py가 없습니다. 프로그램 폴더 전체를 함께 보관해주세요.","음원분리댕"); return; }
        List<string> candidates=new List<string>();
        string configured=Environment.GetEnvironmentVariable("SEPARATOR_PYTHON");
        if(!String.IsNullOrEmpty(configured)) candidates.Add(configured);
        foreach(string path in (Environment.GetEnvironmentVariable("PATH")??"").Split(';')) {
            try { if(!String.IsNullOrWhiteSpace(path)) candidates.Add(Path.Combine(path.Trim('"'),"pythonw.exe")); } catch(ArgumentException) { }
        }
        foreach(RegistryView view in new RegistryView[] { RegistryView.Registry64,RegistryView.Registry32 }) {
            foreach(RegistryHive hive in new RegistryHive[] { RegistryHive.CurrentUser,RegistryHive.LocalMachine }) {
                try {
                    using(RegistryKey root=RegistryKey.OpenBaseKey(hive,view))
                    using(RegistryKey python=root.OpenSubKey(@"SOFTWARE\Python\PythonCore")) {
                        if(python!=null) foreach(string version in python.GetSubKeyNames()) {
                            using(RegistryKey install=python.OpenSubKey(version+@"\InstallPath")) {
                                if(install!=null) {
                                    string executable=install.GetValue("WindowedExecutablePath") as string;
                                    if(!String.IsNullOrEmpty(executable)) candidates.Add(executable);
                                    string location=install.GetValue(null) as string;
                                    if(!String.IsNullOrEmpty(location)) candidates.Add(Path.Combine(location,"pythonw.exe"));
                                }
                            }
                        }
                    }
                } catch(System.Security.SecurityException) { }
            }
        }
        candidates.Add(@"C:\python\pythonw.exe");
        foreach(string candidate in candidates) {
            if(!File.Exists(candidate)) continue;
            try {
                ProcessStartInfo start=new ProcessStartInfo(candidate,"\""+app+"\"");
                start.WorkingDirectory=folder; start.UseShellExecute=false; start.CreateNoWindow=true;
                start.EnvironmentVariables["PYTHONUTF8"]="1";
                Process.Start(start); return;
            } catch(Exception) { }
        }
        MessageBox.Show("Python 3.10 또는 3.11 (64비트)이 필요합니다.\nPython을 설치한 후 다시 실행해주세요.\n\n공식 다운로드: https://www.python.org/downloads/windows/", "음원분리댕",MessageBoxButtons.OK,MessageBoxIcon.Information);
    }
}
