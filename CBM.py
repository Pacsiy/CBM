#!/usr/bin/python

from __future__ import print_function
import keyboard
import logging
import time
import socket
import argparse
import os
import sys
import errno
import stat
import signal
from contextlib import closing

#import Gtk+
from gi import require_version
require_version("Gtk", "3.0")
from gi.repository import Gtk, Gdk, GLib, GObject
try:
    require_version("Wnck", "3.0")
    from gi.repository import Wnck
except (ImportError, ValueError):
    Wnck = None

FileNotFoundError = EnvironmentError
FileExistsError = ProcessLookupError = OSError

# define CBM specific error
class CbmError(Exception):
    def __init__(self, args="Cbm Error."):
        Exception.__init__(self, args)

class suppress_if_errno(object):
    def __init__(self, exceptions, exc_val):
        self._exceptions = exceptions
        self._exc_val = exc_val

    def __enter__(self):
        pass

    def __exit__(self, exctype, excinst, exctb):
        return exctype is not None and issubclass(exctype, self._exceptions) and excinst.errno == self._exc_val


# Daemon class
class Daemon:
    def __init__(self, args):
        self.args = args
        self.sock_file = args.socket_file
        self.sock = None
        self.clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.board = []
        self.client_msgs = {}

    def owner_change(self, board, event):
        text = self.clipboard.wait_for_text()
        text = safe_decode(text)
        try:
            self.board.index(text)
        except ValueError:
            self.board.append(text)

    def exit(self):
        logging.debug("Daemon quiting...")
        try:
            os.unlink(self.sock_file)
        except FileNotFoundError:
            logging.warning("Failed to remove socket file: %s", self.sock_file)
        Gtk.main_quit()

    def run(self):
        self.window = Gtk.Window(type=Gtk.WindowType.POPUP)
        self.c_id = self.clipboard.connect('owner-change',
                                           self.owner_change)

        with suppress_if_errno(FileNotFoundError, errno.ENOENT):
            os.unlink(self.sock_file)

        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.sock_file)
        os.chmod(self.sock_file, stat.S_IRUSR | stat.S_IWUSR)
        self.sock.listen(5)

        #keyboard.add_hotkey('ctrl+c', self.on_copy)
        #keyboard.add_hotkey('ctrl+x', self.on_copy)

        # Handle socket connections
        GObject.io_add_watch(self.sock, GObject.IO_IN,
                             self.socket_accept)
        # Handle unix signals
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, self.exit)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGTERM, self.exit)
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGHUP, self.exit)
        Gtk.main()

    def selection_widget(self):
        # Create windows & widgets
        self.window = Gtk.Dialog(title="Cbm", parent=Gtk.Window())
        scrolled = Gtk.ScrolledWindow()
        model = Gtk.ListStore(str, str)
        tree = Gtk.TreeView(model)
        tree_select = tree.get_selection()
        tree_select.set_mode(Gtk.SelectionMode.MULTIPLE)
        renderer = Gtk.CellRendererText()
        column = Gtk.TreeViewColumn("[ret] to activate, [del] to remove, [esc] to exit.",
                                    renderer, markup=0)

        # Add rows to the model
        for item in self.board:
            label = GLib.markup_escape_text(item)
            row_height = 20
            trunc = ""
            lines = label.splitlines(True)
            if len(lines) > row_height + 1:
                trunc = "<b><i>({0} more lines)</i></b>".format(len(lines) - row_height)
            label = "{0}{1}".format(''.join(lines[:row_height]), trunc)
            # Add label and full text to model
            model.append([label, item])

        # Format, connect and show windows
        # Allow alternating color for rows, if WM theme supports it
        tree.set_rules_hint(True)
        # Draw horizontal divider lines between rows
        tree.set_grid_lines(Gtk.TreeViewGridLines.HORIZONTAL)

        tree.append_column(column)
        scrolled.add(tree)

        # Handle keypresses
        self.window.connect("key-press-event", self.keypress_handler, tree_select)

        # Handle window delete event
        self.window.connect('delete_event', self.window.hide)

        # Add a 'select' button
        select_btn = Gtk.Button.new_with_label("Select")
        select_btn.connect("clicked", self.activate_handler, tree_select)

        # Add a box to hold buttons
        button_box = Gtk.Box()
        button_box.pack_start(select_btn, True, False, 0)

        # GtkDialog comes with a vbox already active, so pack into this
        self.window.vbox.pack_start(scrolled, True, True, 0)
        self.window.vbox.pack_start(button_box, False, False, 0)
        self.window.set_size_request(500, 500)
        self.window.show_all()

    def keypress_handler(self, widget, event, tree_select):
        """Handle selection_widget keypress events."""

        # Handle select with return or mouse
        if event.keyval == Gdk.KEY_Return:
            self.activate_handler(event, tree_select)
        # Delete items from history
        if event.keyval == Gdk.KEY_Delete:
            self.delete_handler(event, tree_select)
        # Hide window if ESC is pressed
        if event.keyval == Gdk.KEY_Escape:
            self.window.hide()

    def delete_handler(self, event, tree_select):
        """Delete selected history entries."""

        model, treepaths = tree_select.get_selected_rows()
        for tree in treepaths:
            treeiter = model.get_iter(tree)
            item = model[treeiter][1]
            item = safe_decode(item)
            logging.debug("Deleting history entry: %s", item)
            
            del self.board[self.board.index(item)]

            # Remove entry from UI
            model.remove(treeiter)

    def activate_handler(self, event, tree_select):
        """Action selected history items."""

        # Get selection
        model, treepaths = tree_select.get_selected_rows()

        # Step over list in reverse, moving to top of board
        if len(treepaths) > 0:
            tree = treepaths[0]
            data = model[model.get_iter(tree)][1]
            self.clipboard.set_text(data, -1)
            logging.debug("%d bytes copied" % len(data))

        model.clear()
        self.window.hide()

    def socket_accept(self, sock, _):
        """Accept a connection and 'select' it for readability."""

        conn, _ = sock.accept()
        self.client_msgs[conn.fileno()] = []
        GObject.io_add_watch(conn, GObject.IO_IN,
                             self.socket_recv)
        logging.debug("Client connection received.")
        return True

    def socket_recv(self, conn, _):
        """Recv from an accepted connection."""

        max_input = 50000
        recv_total = sum(len(x) for x in self.client_msgs[conn.fileno()])
        try:
            recv = safe_decode(conn.recv(min(8192, max_input - recv_total)))
            self.client_msgs[conn.fileno()].append(recv)
            recv_total += len(recv)
            if not recv or recv_total >= max_input:
                self.process_msg(conn)
            else:
                return True
        except socket.error as exc:
            logging.error("Socket error %s", exc)
            logging.debug("Exception:", exc_info=True)

        conn.close()
        # Return false to remove conn from GObject.io_add_watch list
        return False

    def process_msg(self, conn):
        """Process message received from client, sending reply if required."""

        try:
            msg_str = ''.join(self.client_msgs.pop(conn.fileno()))
        except KeyError:
            return
        
        if msg_str == 'PASTE':
            self.selection_widget()

# Client class
class Client:
    def __init__(self, args):
        self.args = args
        self.sock_file = args.socket_file
    def run(self):
        logging.debug("Connecting to server to update.")
        with closing(socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)) as sock:
            try:
                sock.connect(self.sock_file)
            except (socket.error, OSError):
                raise CbmError("Error connecting to socket. Is daemon running?")
            logging.debug("Sending request to server.")
            # Fix for http://bugs.python.org/issue1633941 in py 2.x
            # Send message 'header' - count is 0 (i.e to be ignored)
            sock.sendall("PASTE".encode('utf-8'))

# some utilities
def safe_decode(data):
    try:
        data = data.decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError, AttributeError):
        pass
    return data

def parse_args():
    parser = argparse.ArgumentParser(description='CBM')
    parser.add_argument('-d', '--daemon', action="store_true",
                        help="Launch daemon program")
    parser.add_argument('-l', '--log_level', action="store", default="INFO",
                        help="Set log level: DEBUG, INFO (default), WARNING, ERROR, CRITICAL")
    parser.add_argument('-s', '--socket_file', action="store", default="./cbm_sock",
                        help="Set socket file location")
    return parser.parse_args()

def main():
    args = parse_args()

    logging.basicConfig(format='%(levelname)s:%(message)s',
                        level=getattr(logging, args.log_level.upper()))
    logging.debug("Debugging Enabled.")

    if args.daemon:
        Daemon(args).run()
    else:
        Client(args).run()

if __name__ == '__main__':
    try:
        main()
    except CbmError as exc:
        if logging.getLogger().getEffectiveLevel() == logging.DEBUG:
            raise
        else:
            logging.error(exc)
            sys.exit(1)