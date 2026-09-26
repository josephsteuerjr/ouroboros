"""Windows desktop tray lifetime: one STA icon pump per visible launcher window.

No WinForms import occurs until start(). A failed icon/pump leaves ordinary close
available; shutdown from the tray uses the launcher's existing stop callback.
"""

import logging
import threading

log = logging.getLogger("launcher.tray")


class WindowsTray:
    def __init__(self, get_window, exit_launcher, shutdown_event):
        self.get_window = get_window
        self.exit_launcher = exit_launcher
        self.shutdown_event = shutdown_event
        self.ready = threading.Event()
        self._stop = threading.Event()
        self._hidden = False
        self._state_lock = threading.Lock()

    def start(self):
        """Start the STA message pump; return false if WinForms cannot load."""
        try:
            import clr
            clr.AddReference("System.Windows.Forms")
            clr.AddReference("System.Drawing")
            clr.AddReference("System.Threading")
            from System.Drawing import Icon, SystemIcons
            from System.Threading import ApartmentState, Thread, ThreadStart
            from System.Windows.Forms import (Application, ApplicationContext, ContextMenuStrip,
                                              MouseButtons, NotifyIcon, Timer, ToolStripMenuItem)
        except Exception:
            log.warning("Tray unavailable; window close will exit normally.", exc_info=True)
            return False

        def restore(sender=None, args=None):
            window = self.get_window()
            if window is not None:
                window.show()
                with self._state_lock:
                    self._hidden = False

        def exit_clicked(sender, args):
            self.ready.clear()
            self.exit_launcher()

        def pump():
            icon = None
            timer = None
            owned_icon = None
            try:
                menu = ContextMenuStrip()
                open_item = ToolStripMenuItem("Open Ouroboros")
                open_item.Click += restore
                exit_item = ToolStripMenuItem("Exit")
                exit_item.Click += exit_clicked
                menu.Items.Add(open_item)
                menu.Items.Add(exit_item)
                icon = NotifyIcon()
                icon.Icon = SystemIcons.Application
                from ouroboros.platform_layer import bundled_resource_bases
                for base in bundled_resource_bases():
                    candidate = base / "assets" / "icon.ico"
                    if candidate.is_file():
                        try:
                            owned_icon = Icon(str(candidate))
                            icon.Icon = owned_icon
                            break
                        except Exception:
                            log.warning("Tray icon asset unreadable: %s", candidate, exc_info=True)
                icon.Text = "Ouroboros — running"
                icon.ContextMenuStrip = menu
                def mouse_click(sender, args):
                    if args.Button == MouseButtons.Left:
                        restore()

                icon.MouseClick += mouse_click
                icon.MouseDoubleClick += restore
                context = ApplicationContext()
                timer = Timer()
                timer.Interval = 200

                def tick(sender, args):
                    if self._stop.is_set() or self.shutdown_event.is_set():
                        Application.ExitThread()  # must run on the STA pump thread
                    elif icon.Visible:
                        self.ready.set()  # pump has actually processed a message

                timer.Tick += tick
                timer.Start()
                icon.Visible = True
                Application.Run(context)
            except Exception:
                log.warning("Tray pump failed; window close will exit normally.", exc_info=True)
            finally:
                for cleanup in (
                    (lambda: timer.Stop(), lambda: timer.Dispose()) if timer is not None else (),
                    (lambda: setattr(icon, "Visible", False), lambda: icon.Dispose()) if icon is not None else (),
                    (lambda: owned_icon.Dispose(),) if owned_icon is not None else (),
                ):
                    for operation in cleanup:
                        try:
                            operation()
                        except Exception:
                            log.warning("Tray cleanup failed.", exc_info=True)
                self._pump_stopped()

        try:
            thread = Thread(ThreadStart(pump))
            thread.SetApartmentState(ApartmentState.STA)
            thread.Start()
        except Exception:
            log.warning("Tray STA thread could not start.", exc_info=True)
            return False
        return True

    def stop(self):
        self.ready.clear()
        self._stop.set()

    def _pump_stopped(self):
        """A lost icon cannot leave a hidden window unreachable."""
        with self._state_lock:
            self.ready.clear()
            must_restore = self._hidden and not self._stop.is_set() and not self.shutdown_event.is_set()
        if must_restore:
            try:
                window = self.get_window()
                if window is not None:
                    window.show()
                    with self._state_lock:
                        self._hidden = False
            except Exception:
                log.error("Tray stopped after window was hidden; restoring window failed.", exc_info=True)

    def hide_on_close(self, window):
        """Hide only with a live icon. Caller returns False to cancel pywebview 5's closing event."""
        with self._state_lock:
            if not self.ready.is_set() or self.shutdown_event.is_set():
                return False
            window.hide()
            self._hidden = True
            return True
