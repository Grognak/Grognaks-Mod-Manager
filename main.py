#!/usr/bin/env python

# Do some basic imports, and set up logging to catch ImportError.
import inspect
import locale
import logging
import os
import sys

if (__name__ == "__main__"):
    global dir_self
    locale.setlocale(locale.LC_ALL, "")

    # Get the un-symlinked, absolute path to this module.
    dir_self = os.path.realpath(os.path.abspath(os.path.split(inspect.getfile( inspect.currentframe() ))[0]))
    if (dir_self not in sys.path): sys.path.insert(0, dir_self)

    # Go to this module's dir.
    os.chdir(dir_self)

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    logstream_handler = logging.StreamHandler()
    logstream_formatter = logging.Formatter("%(levelname)s: %(message)s")
    logstream_handler.setFormatter(logstream_formatter)
    logstream_handler.setLevel(logging.INFO)
    logger.addHandler(logstream_handler)

    logfile_handler = logging.FileHandler(os.path.join(dir_self, "modman-log.txt"), mode="w")
    logfile_formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    logfile_handler.setFormatter(logfile_formatter)
    logger.addHandler(logfile_handler)

    # __main__ stuff is continued at the end of this file.


# Import everything else (tkinter may be absent in some environments).
try:
    from ConfigParser import SafeConfigParser
    import errno
    import glob
    import platform
    import Queue
    import re
    import shutil as sh
    import subprocess
    import tempfile as tf
    import threading
    import time
    import webbrowser
    import zipfile as zf
    import Tkinter as tk
    import tkFileDialog
    import tkMessageBox as msgbox

    # Modules bundled with this script.
    from lib.ftldat import FTLDatUnpacker
    from lib.ftldat import FTLDatPacker
    from lib import killable_threading

except (Exception) as err:
    logging.exception(err)
    sys.exit(1)


# Declare globals.
APP_VERSION = "1.6.1"
APP_NAME = "Grognak's Mod Manager v%s" % APP_VERSION
allowzip = False
never_run_ftl = False
dir_mods = None
dir_res = None


class RootWindow(tk.Tk):
    def __init__(self, master, *args, **kwargs):
        tk.Tk.__init__(self, master, *args, **kwargs)
        # Pseudo enum constants.
        self.ACTIONS = ["ACTION_CONFIG", "ACTION_SHOW_MAIN_WINDOW",
                        "ACTION_PATCHING_SUCCEEDED", "ACTION_PATCHING_FAILED",
                        "ACTION_DIE"]
        for x in self.ACTIONS: setattr(self, x, x)

        self._event_queue = Queue.Queue()
        self.done = False  # Indicates to other threads that mainloop() ended.
        self._main_window = None

        self.wm_protocol("WM_DELETE_WINDOW", self._on_delete)

        def poll_queue():
            self._process_event_queue(None)
            self._poll_queue_alarm_id = self.after(100, self._poll_queue)
        self._poll_queue_alarm_id = None
        self._poll_queue = poll_queue

        self._poll_queue()

    def _on_delete(self):
        if (self._poll_queue_alarm_id is not None):
            self.after_cancel(self._poll_queue_alarm_id)
        self._root().quit()

    def _process_event_queue(self, event):
        """Processes every pending event on the queue."""
        # With after() polling, always use get_nowait() to avoid blocking.
        func_or_name, arg_dict = None, None
        while (True):
            try:
                func_or_name, arg_dict = self._event_queue.get_nowait()
            except (Queue.Empty) as err:
                break
            else:
                self._process_event(func_or_name, arg_dict)

    def _process_event(self, func_or_name, arg_dict):
        """Processes events queued via invoke_later()."""
        global APP_NAME
        global dir_self, dir_res

        def check_args(args):
            for arg in args:
                if (arg not in arg_dict):
                    logging.error("Missing %s arg queued to %s %s." % (arg, self.__class__.__name__, func_or_name))
                    return False
            return True

        if (hasattr(func_or_name, "__call__")):
            func_or_name(arg_dict)

        elif (func_or_name == self.ACTION_CONFIG):
            check_args(["write_config", "config_parser", "next_func"])

            if (dir_res):
                if (not msgbox.askyesno(APP_NAME, "FTL resources were found in:\n%s\nIs this correct?" % dir_res)):
                    dir_res = None

            if (not dir_res):
                logging.debug("FTL dats path was not located automatically. Prompting user for location.")
                dir_res = prompt_for_ftl_path()

            if (dir_res):
                arg_dict["config_parser"].set("settings", "ftl_dats_path", dir_res)
                arg_dict["write_config"] = True
                logging.info("FTL dats located at: %s" % dir_res)

            if (not dir_res):
                logging.debug("No FTL dats path found, exiting.")
                sys.exit(1)

            arg_dict["next_func"]({"write_config":arg_dict["write_config"], "config_parser":arg_dict["config_parser"]})

        elif (func_or_name == self.ACTION_SHOW_MAIN_WINDOW):
            check_args(["mod_names", "next_func"])

            if (not self._main_window):
                self._main_window = MainWindow(master=self, title=APP_NAME, mod_names=arg_dict["mod_names"], next_func=arg_dict["next_func"])
                self._main_window.center_window()

        elif (func_or_name == self.ACTION_PATCHING_SUCCEEDED):
            check_args(["ftl_exe_path"])

            if (arg_dict["ftl_exe_path"]):
                if (msgbox.askyesno(APP_NAME, "Patching completed successfully. Run FTL now?")):
                    logging.info("Running FTL...")
                    subprocess.Popen([arg_dict["ftl_exe_path"]])
            else:
                msgbox.showinfo(APP_NAME, "Patching completed successfully.")

        elif (func_or_name == self.ACTION_PATCHING_FAILED):
                msgbox.showerror(APP_NAME, "Patching failed. See log for details.")

        elif (func_or_name == self.ACTION_DIE):
            # Destruction awaits. Nothing more to do.
            with self._event_queue.mutex:
                self._event_queue.queue.clear()
            self._root().destroy()

    def invoke_later(self, func_or_name, arg_dict):
        """Schedules an action to occur in this thread. (thread-safe)"""
        self._event_queue.put((func_or_name, arg_dict))
        # The queue will be polled eventually by an after() alarm.


class MainWindow(tk.Toplevel):
    def __init__(self, master, *args, **kwargs):
        self.custom_args = {"title":None, "mod_names":[], "next_func":None}
        for k in self.custom_args.keys():
          if (k in kwargs):
            self.custom_args[k] = kwargs[k]
            del kwargs[k]
        tk.Toplevel.__init__(self, master, *args, **kwargs)

        if (self.custom_args["title"]): self.wm_title(self.custom_args["title"])

        self.button_width = 7
        self.button_padx = "2m"
        self.button_pady = "1m"

        self._prev_selection = set()
        self._mouse_press_list_index = None

        self._reordered_mods = None
        self._pending_mods = None

        self.resizable(False, False)

        # Our topmost frame is called root_frame.
        root_frame = tk.Frame(self)
        root_frame.pack()

        # Top frame (container).
        top_frame = tk.Frame(root_frame)
        top_frame.pack(side="top", fill="both", expand="yes")

        # Top-left frame (mod list).
        left_frame = tk.Frame(top_frame, #background="red",
            borderwidth=1, relief="ridge",
            width=50, height=250)
        left_frame.pack(side="left", fill="both", expand="yes")

        # Top-right frame (buttons).
        right_frame = tk.Frame(top_frame, width=250)
        right_frame.pack(side="right", fill="y", expand="no")

        # Bottom frame (mod descriptions).
        bottom_frame = tk.Frame(root_frame,
            borderwidth=3, relief="ridge",
            height=50)
        bottom_frame.pack(side="top", fill="both", expand="yes")

        # Add a listbox to hold the mod names.
        self._mod_listbox = tk.Listbox(left_frame, width=30, height=1, selectmode="multiple") # Height readjusts itself for the button frame
        self._mod_listbox.pack(side="left", fill="both", expand="yes")
        self._mod_scrollbar = tk.Scrollbar(left_frame, command=self._mod_listbox.yview, orient="vertical")
        self._mod_scrollbar.pack(side="right", fill="y")
        self._mod_listbox.bind("<<ListboxSelect>>", self._on_listbox_select)
        self._mod_listbox.bind("<Button-1>", self._on_listbox_mouse_pressed)
        self._mod_listbox.bind("<B1-Motion>", self._on_listbox_mouse_dragged)
        self._mod_listbox.configure(yscrollcommand=self._mod_scrollbar.set)

        # Add textbox at bottom to hold mod information.
        self._desc_area = tk.Text(bottom_frame, width=60, height=10, wrap="word")
        self._desc_area.pack(fill="both", expand="yes")

        # Set formating tags.
        self._desc_area.tag_configure("title", font="helvetica 24 bold")

        # Add the buttons to the buttons frame.
        self._patch_btn = tk.Button(right_frame, text="Patch")
        self._patch_btn.configure(
            width=self.button_width,
            padx=self.button_padx, pady=self.button_pady)

        self._patch_btn.pack(side="top")
        self._patch_btn.configure(command=self._patch)
        self._patch_btn.bind("<Return>", lambda e: self._patch())

        self._toggle_all_btn = tk.Button(right_frame, text="Toggle All")
        self._toggle_all_btn.configure(
            width=self.button_width,
            padx=self.button_padx, pady=self.button_pady)

        self._toggle_all_btn.pack(side="top")
        self._toggle_all_btn.configure(command=self._toggle_all)
        self._toggle_all_btn.bind("<Return>", lambda e: self._toggle_all())

        self._dummy_a_btn = tk.Button(right_frame, text="")
        self._dummy_a_btn.configure(
            width=self.button_width,
            padx=self.button_padx, pady=self.button_pady,
            state="disabled")

        self._dummy_a_btn.pack(side="top")

        self._forum_btn = tk.Button(right_frame, text="Forum")
        self._forum_btn.configure(
            width=self.button_width,
            padx=self.button_padx, pady=self.button_pady)

        self._forum_btn.pack(side="top")
        self._forum_btn.configure(command=self._browse_forum)
        self._forum_btn.bind("<Return>", lambda e: self._browse_forum())

        self._fill_list()
        self.wm_protocol("WM_DELETE_WINDOW", self._destroy)  # Intercept window manager closing.

        self._patch_btn.focus_force()

    def _fill_list(self):
        """Fills the list of all available mods."""
        global APP_VERSION

        # Set default description.
        self._set_description("Grognak's Mod Manager", "Grognak", APP_VERSION, "Thanks for using GMM. Make sure to periodically check the forum for updates!")

        for mod_name in self.custom_args["mod_names"]:
            self._add_mod(mod_name, False)

    def _on_listbox_select(self, event):
        current_selection = self._mod_listbox.curselection()
        new_selection = [x for x in current_selection if (x not in self._prev_selection)]
        self._prev_selection = set(current_selection)

        if (len(new_selection) > 0):
            self._set_description(self._mod_listbox.get(new_selection[0]))

    def _on_listbox_mouse_pressed(self, event):
        self._mouse_press_list_index = self._mod_listbox.nearest(event.y)

    def _on_listbox_mouse_dragged(self, event):
        if (self._mouse_press_list_index is None): return

        current_selection = [int(i) for i in self._mod_listbox.curselection()]
        n = self._mod_listbox.nearest(event.y)
        if (n < self._mouse_press_list_index):
            x = self._mod_listbox.get(n)
            self._mod_listbox.delete(n)
            self._mod_listbox.insert(n+1, x)
            if ((n) in current_selection): self._mod_listbox.selection_set(n+1)
            self._mouse_press_list_index = n
        elif (n > self._mouse_press_list_index):
            x = self._mod_listbox.get(n)
            self._mod_listbox.delete(n)
            self._mod_listbox.insert(n-1, x)
            if ((n) in current_selection): self._mod_listbox.selection_set(n-1)
            self._mouse_press_list_index = n

    def _add_mod(self, modname, selected):
        """Add a mod name to the list."""
        newitem = self._mod_listbox.insert(tk.END, modname)
        if (selected):
            self._mod_listbox.selection_set(newitem)

    def _set_description(self, title, author=None, version=None, description=None):
        """Sets the currently displayed mod description."""
        self._desc_area.configure(state="normal")
        self._desc_area.delete("1.0", tk.END)
        self._desc_area.insert(tk.END, (title +"\n"), "title")
        if (author is not None and version is not None):
            self._desc_area.insert(tk.END, "by %s (version %s)\n\n" % (author, str(version)))
        else:
            self._desc_area.insert(tk.END, "\n")
        if (description):
            self._desc_area.insert(tk.END, description)
        else:
            self._desc_area.insert(tk.END, "No description.")
        self._desc_area.configure(state="disabled")

    def _patch(self):
        # Remember the names to return in _on_delete().
        self._reordered_mods = self._mod_listbox.get(0, tk.END)
        self._pending_mods = [self._mod_listbox.get(n) for n in self._mod_listbox.curselection()]
        self._destroy()

    def _toggle_all(self):
        """Select all mods, or none if all are already selected."""
        current_selection = self._mod_listbox.curselection()
        if (len(current_selection) == self._mod_listbox.size()):
            self._mod_listbox.selection_clear(0, tk.END)
        else:
            self._mod_listbox.selection_set(0, tk.END)

    def _browse_forum(self):
        webbrowser.open("http://www.ftlgame.com/forum/viewtopic.php?f=12&t=2464")

    def center_window(self):
        """Centers this window on the screen.
        Mostly. Window manager decoration and the menubar aren't factored in.
        """
        # An event-driven call to this would go nuts with plain update().
        self.update_idletasks()  # Make window width/height methods work.
        xp = (self.winfo_screenwidth()//2) - (self.winfo_width()//2)
        yp = (self.winfo_screenheight()//2) - (self.winfo_height()//2)
        self.geometry("+%d+%d" % (xp, yp))

    def _on_delete(self):
        # The window manager closed this window.
        self._destroy()

    def _destroy(self):
        """Destroys this window, but triggers a callback first."""
        # If patch was clicked, names will be returned. Otherwise None.
        if (self.custom_args["next_func"]):
            self._root().invoke_later(self.custom_args["next_func"], {"all_mods":self._reordered_mods, "selected_mods":self._pending_mods})
        self.destroy()


def ftl_path_join(*args):
    """ Joins paths in the way FTL expects them to be in .dat files.
        That is: the UNIX way. """
    return "/".join(args)

def appendfile(src, dst):
    source = open(src, "r")
    target = open(dst, "a")

    target.write(source.read() +"\n")

    source.close()
    target.close()

def mergefile(src, dst):
    pass

def packdat(unpack_dir, dat_path):
    logging.info("")
    logging.info("Repacking %s" % os.path.basename(dat_path))
    logging.info("Listing files to pack...")
    s = [()]
    files = []
    while s:
        current = s.pop()
        for child in os.listdir(os.path.join(unpack_dir, *current)):
            full_path = os.path.join(unpack_dir, *(current + (child,)))
            if (os.path.isfile(full_path)):
                files.append(current + (child,))
            elif (os.path.isdir(full_path)):
                s.append(current + (child,))
    logging.info("Creating datfile...")
    index_size = len(files)
    packer = FTLDatPacker(open(dat_path, "wb"), index_size)
    logging.info("Packing...")
    for _file in files:
        full_path = os.path.join(unpack_dir, *_file)
        size = os.stat(full_path).st_size
        with open(full_path, "rb") as f:
            packer.add(ftl_path_join(*_file), f, size)

def unpackdat(dat_path, unpack_dir):
    logging.info("Unpacking %s..." % os.path.basename(dat_path))
    unpacker = FTLDatUnpacker(open(dat_path, "rb"))

    for i, filename, size, offset in unpacker:
        target = os.path.join(unpack_dir, filename)
        if (not os.path.exists(os.path.dirname(target))):
            os.makedirs(os.path.dirname(target))
        with open(target, "wb") as f:
            unpacker.extract_to(filename, f)

def is_dats_path_valid(dats_path):
    """Returns True if the path exists and contains data.dat."""
    return (os.path.isdir(dats_path) and os.path.isfile(os.path.join(dats_path, "data.dat")))

def find_ftl_path():
    """Returns a valid guessed path to FTL resources, or None."""
    steam_chunks = ["Steam","steamapps","common","FTL Faster Than Light","resources"];
    gog_chunks = ["GOG.com","Faster Than Light","resources"];

    home_path = os.path.expanduser("~")

    xdg_data_home = os.getenv("XDG_DATA_HOME")
    if (not xdg_data_home):
        xdg_data_home = os.path.join(home_path, *[".local","share"])

    candidates = []
    # Windows - Steam
    candidates.append(os.path.join(os.getenv("ProgramFiles(x86)", ""), *steam_chunks))
    candidates.append(os.path.join(os.getenv("ProgramFiles", ""), *steam_chunks))
    # Windows - GOG
    candidates.append(os.path.join(os.getenv("ProgramFiles(x86)", ""), *gog_chunks))
    candidates.append(os.path.join(os.getenv("ProgramFiles", ""), *gog_chunks))
    # Linux - Steam
    candidates.append(os.path.join(xdg_data_home, *["Steam","SteamApps","common","FTL Faster Than Light","data","resources"]))
    # OSX - Steam
    candidates.append(os.path.join(home_path, *["Library","Application Support","Steam","SteamApps","common","FTL Faster Than Light","FTL.app","Contents","Resources"]))
    # OSX
    candidates.append(os.path.join("/", *["Applications","FTL.app","Contents","Resources"]))

    for c in candidates:
        if (is_dats_path_valid(c)):
            return c

    return None

def prompt_for_ftl_path():
    """Returns a path to FTL resources chosen by the user, or None.
    This should be called from a tkinter gui mainloop.
    """
    global APP_NAME

    message = ""
    message += "The path to FTL's resources could not be guessed.\n\n";
    message += "You will now be prompted to locate FTL manually.\n";
    message += "Select '(FTL dir)/resources/data.dat'.\n";
    message += "Or 'FTL.app', if you're on OSX.";
    msgbox.showinfo(APP_NAME, message)

    result = tkFileDialog.askopenfilename(title="Find data.dat or FTL.app",
        filetypes=[("data.dat or OSX Bundle", "data.dat;FTL.app")])

    if (result):
        if (os.path.basename(result) == "data.dat"):
            result = os.path.split(result)[0]
        elif (os.path.splitext(result)[1] == ".app" and os.path.isdir(result)):
            if (os.path.isdir(os.path.join(result, *["Contents","Resources"]))):
                result = os.path.join(result, *["Contents","Resources"])
        if (result and is_dats_path_valid(result)):
            return result

    return None

def find_mod(mod_name):
    """Returns the full path to a mod file, or None."""
    global allowzip
    global dir_mods

    suffixes = ["", ".ftl"]
    if (allowzip): suffixes.append(".zip")

    for suffix in suffixes:
        tmp_path = os.path.join(dir_mods, mod_name + suffix)
        if (os.path.isfile(tmp_path)):
            return tmp_path

    logging.debug("Failed to find mod file by name: %s" % mod_name)
    return None

def load_modorder():
    """Reads the modorder, syncs it with existing files, and returns it."""
    global allowzip
    global dir_mods

    modorder_lines = []
    try:
        # Translate mod names to full filenames, temporarily.
        with open(os.path.join(dir_mods, "modorder.txt"), "r") as modorder_file:
            modorder_lines = modorder_file.readlines()
            modorder_lines = [line.strip() for line in modorder_lines]
            modorder_lines = [find_mod(line) for line in modorder_lines]
            modorder_lines = [line for line in modorder_lines if (line is not None)]
            modorder_lines = [os.path.basename(line) for line in modorder_lines]
    except (IOError) as err:
        # Ignore "No such file/dir."
        if (err.errno == errno.ENOENT): pass
        else: raise

    mod_exts = ["ftl"]
    if (allowzip): mod_exts.append("zip")

    # Get a list of full filenames.
    mod_filenames = []
    for ext in mod_exts:
        ext_filenames = glob.glob(os.path.join(dir_mods, "*."+ext))
        ext_filenames = [os.path.basename(f) for f in ext_filenames]
        mod_filenames.extend(ext_filenames)

    # Purge modorder lines that have no corresponding filename.
    dead_mods = [f for f in modorder_lines if (f not in mod_filenames)]
    for f in dead_mods:
        modorder_lines.remove(f)
        logging.info("Removed %s" % f)

    # Append filenames not mentioned in the modorder.
    new_mods = [f for f in mod_filenames if (f not in modorder_lines)]
    for f in new_mods:
        modorder_lines.append(f)
        logging.info("Added %s" % f)

    # Strip extensions to get mod_names.
    ext_ptn = "[.](?:"+ ("|".join(mod_exts)) +")$"
    modorder_lines = [re.sub(ext_ptn, "", f, flags=re.I) for f in modorder_lines]

    return modorder_lines

def save_modorder(modorder_lines):
    global dir_mods
    with open(os.path.join(dir_mods, "modorder.txt"), "w") as modorder_file:
        modorder_file.write("\n".join(modorder_lines) +"\n")

def patch_dats(selected_mods, keep_alive_func=None, sleep_func=None):
    """Backs up, clobbers, unpacks, merges, and finally packs dats.

    :param selected_mods: A list of mod names to install.
    :param keep_alive_func: Optional replacement to get an abort boolean.
    :param sleep_func: Optional replacement to sleep N seconds.
    :return: True if successful, False otherwise.
    """
    global allowzip
    global dir_self, dir_mods, dir_res

    if (keep_alive_func is None): keep_alive_func = lambda : True
    if (sleep_func is None): sleep_func = time.sleep

    # Get full paths from mod names.
    mod_list = [find_mod(mod_name) for mod_name in selected_mods]
    mod_list = [mod_path for mod_path in mod_list if (mod_path is not None)]

    data_dat_path = os.path.join(dir_res, "data.dat")
    resource_dat_path = os.path.join(dir_res, "resource.dat")

    data_bak_path = os.path.join(dir_res, "data.dat.bak")
    resource_bak_path = os.path.join(dir_res, "resource.dat.bak")

    data_unp_path = None
    resource_unp_path = None
    tmp = None

    try:
        # Create backup dats, if necessary.
        for (dat_path, bak_path) in [(data_dat_path,data_bak_path), (resource_dat_path,resource_bak_path)]:
            if (not os.path.isfile(bak_path)):
                logging.info("Backing up %s" % os.path.basename(dat_path))
                sh.copy2(dat_path, bak_path)

        if (not keep_alive_func()): return False

        # Clobber current dat files with their respective backups.
        for (dat_path, bak_path) in [(data_dat_path,data_bak_path), (resource_dat_path,resource_bak_path)]:
            sh.copy2(bak_path, dat_path)

        if (not keep_alive_func()): return False

        if (len(mod_list) == 0):
            return True  # No mods. Skip the repacking.

        # Use temp folders for unpacking.
        data_unp_path = tf.mkdtemp()
        resource_unp_path = tf.mkdtemp()

        unp_map = {}
        for x in ["data"]:
            unp_map[x] = data_unp_path
        for x in ["audio", "fonts", "img"]:
            unp_map[x] = resource_unp_path

        # Extract both of the dats.
        unpackdat(data_dat_path, data_unp_path)
        unpackdat(resource_dat_path, resource_unp_path)

        # Extract each mod into a temp dir and merge into unpacked dat dirs.
        for mod_path in mod_list:
            if (not keep_alive_func()): return False
            try:
                logging.info("")
                logging.info("Installing mod: %s" % os.path.basename(mod_path))
                tmp = tf.mkdtemp()
                with zf.ZipFile(mod_path, "r") as mod_zip:
                    # Unzip everything into a temporary folder.
                    for item in mod_zip.namelist():
                        if (item.endswith("/")):
                            path = os.path.join(tmp, item)
                            if (not os.path.exists(path)):
                                os.makedirs(path)
                        else:
                            mod_zip.extract(item, tmp)

                # Go through each directory in the mod.
                for directory in os.listdir(tmp):
                    if (directory in unp_map):
                        logging.info("Merging folder: %s" % directory)
                        unpack_dir = unp_map[directory]
                        for root, dirs, files in os.walk(os.path.join(tmp, directory)):
                            for d in dirs:
                                path = os.path.join(unpack_dir, root[len(tmp)+1:], d)
                                if (not os.path.exists(path)):
                                    os.makedirs(path)
                            for f in files:
                                if (f.endswith(".append")):
                                    appendfile(os.path.join(root, f), os.path.join(unpack_dir, root[len(tmp)+1:], f[:-len(".append")]))
                                elif (f.endswith(".append.xml")):
                                    appendfile(os.path.join(root, f), os.path.join(unpack_dir, root[len(tmp)+1:], f[:-len(".append.xml")]+".xml"))
                                elif (f.endswith(".merge")):
                                    mergefile(os.path.join(root, f), os.path.join(unpack_dir, root[len(tmp)+1:], f[:-len(".merge")]))
                                elif (f.endswith("merge.xml")):
                                    mergefile(os.path.join(root, f), os.path.join(unpack_dir, root[len(tmp)+1:], f[:-len(".merge.xml")]+".xml"))
                                else:
                                    sh.copy2(os.path.join(root, f), os.path.join(unpack_dir, root[len(tmp)+1:], f))

                    else:
                        logging.warning("Unsupported folder: %s" % directory)

            finally:
                # Clean up temporary mod folder's contents.
                if (tmp is not None):
                    sh.rmtree(tmp, ignore_errors=True)
                    tmp = None

        if (not keep_alive_func()): return False

        # All the mods are installed, so repack the files.
        packdat(data_unp_path, data_dat_path)
        packdat(resource_unp_path, resource_dat_path)

        return True  # The finally block will still run.

    finally:
        # Delete unpack folders.
        for unpack_dir in [data_unp_path, resource_unp_path]:
            if (not unpack_dir): continue

            sh.rmtree(unpack_dir, ignore_errors=True)
            if (os.path.exists(unpack_dir)):
                logging.warning("Failed to delete unpack folder: %s" % unpack_dir)

    return False

def find_ftl_exe():
    """Returns the FTL executable's path, or None."""
    global dir_res

    if (platform.system() == "Windows"):
        exe_path = os.path.normpath(os.path.join(dir_res, *["..", "FTLGame.exe"]))
        if (os.path.isfile(exe_path)):
            return exe_path

    return None


class LogicThread(killable_threading.KillableThread):
    """One thread to rule them all.

    Thanks to invoke_later(), there is no ambiguity concerning what
    thread is running the methods here. Any member variables on this
    class will be safe to reference in its methods.

    Globals should only be used if they're constants, or at least not
    changed while multiple threads are simultaneously looking at them.
    """

    def __init__(self, root_window):
        killable_threading.KillableThread.__init__(self)
        self.ACTIONS = ["ACTION_LOAD_CONFIG", "ACTION_CONFIG_LOADED",
                        "ACTION_MAIN_WINDOW_CLOSED", "ACTION_PATCHING_FINISHED"]
        for x in self.ACTIONS: setattr(self, x, x)

        self._mygui = root_window
        self._patch_thread = None
        self._event_queue = Queue.Queue()

    def run(self):
        try:
            self.invoke_later(self.ACTION_LOAD_CONFIG, {})

            while (self.keep_alive):
                self._process_event_queue(0.5)  # Includes some blocking.
                if (not self.keep_alive): break
                if (self._mygui.done is True): break

        except (Exception) as err:
            logging.exception("Unexpected exception in %s." % self.__class__.__name__)

        # This thread is done.
        self.keep_alive = False
        self._mygui.invoke_later(self._mygui.ACTION_DIE, {})
        if (self._patch_thread):
            self._patch_thread.stop_living()

    def _process_event_queue(self, queue_timeout=None):
        """Processes every pending event on the queue.

        :param queue_timeout: Optionally block up to N seconds in the initial check.
        """
        action_name, arg_dict = None, None
        first_pass = True
        while(True):
            try:
                if (first_pass):
                    queue_block = True if (queue_timeout is not None and queue_timeout > 0) else False
                    action_name, arg_dict = self._event_queue.get(queue_block, queue_timeout)
                else:
                    first_pass = False
                    action_name, arg_dict = self._event_queue.get_nowait()
            except (Queue.Empty):
                break
            else:
                self._process_event(action_name, arg_dict)

    def _process_event(self, action_name, arg_dict):
        """Processes events queued via invoke_later()."""
        if (action_name == self.ACTION_LOAD_CONFIG):
            self._load_config(arg_dict)

        elif (action_name == self.ACTION_CONFIG_LOADED):
            self._config_loaded(arg_dict)

        elif (action_name == self.ACTION_MAIN_WINDOW_CLOSED):
            self._main_window_closed(arg_dict)

        elif (action_name == self.ACTION_PATCHING_FINISHED):
            self._patching_finished(arg_dict)

    def _load_config(self, arg_dict):
        global allowzip, never_run_ftl
        global dir_self, dir_res

        cfg = SafeConfigParser()
        cfg.add_section("settings")

        # Set defaults.
        cfg.set("settings", "allowzip", ("1" if (allowzip is True) else "0"))
        cfg.set("settings", "ftl_dats_path", "")
        cfg.set("settings", "never_run_ftl", ("1" if (never_run_ftl is True) else "0"))

        write_config = False
        try:
            with open(os.path.join(dir_self, "modman.ini"), "r") as cfg_file:
                cfg.readfp(cfg_file)
        except (Exception) as err:
            write_config = True

        if (cfg.has_option("settings", "allowzip")):
            allowzip = cfg.getboolean("settings", "allowzip")

        if (cfg.has_option("settings", "ftl_dats_path")):
            dir_res = cfg.get("settings", "ftl_dats_path")

        if (cfg.has_option("settings", "never_run_ftl")):
            never_run_ftl = cfg.getboolean("settings", "never_run_ftl")

        # Remove deprecated settings.
        for x in ["macmodsdir", "highlightall"]:
          if (cfg.has_option("settings", x)):
            cfg.remove_option("settings", x)

        if (dir_res):
            logging.info("Using FTL dats path from config: %s" % dir_res)
            if (not is_dats_path_valid(dir_res)):
                logging.error("The config's FTL dats path does not exist, or it lacks data.dat.")
                dir_res = None
        else:
            logging.debug("No FTL dats path previously set.")

        if (not dir_res):
            # Find/prompt for the path to set in the config.
            dir_res = find_ftl_path()

            def next_func(arg_dict):
                self.invoke_later(self.ACTION_CONFIG_LOADED, arg_dict)

            self._mygui.invoke_later(self._mygui.ACTION_CONFIG, {"write_config":write_config, "config_parser":cfg, "next_func":next_func})
        else:
            # Skip to the next phase.
            self.invoke_later(self.ACTION_CONFIG_LOADED, {"write_config":write_config, "config_parser":cfg})

    def _config_loaded(self, arg_dict):
        global dir_self

        for arg in ["write_config", "config_parser"]:
            if (arg not in arg_dict):
                logging.error("Missing arg %s for %s.%s()." % (arg, self.__class__.__name__, inspect.stack()[0][3]))
                return

        if (arg_dict["write_config"]):
            with open(os.path.join(dir_self, "modman.ini"), "w") as cfg_file:
                cfg_file.write("#\n")
                cfg_file.write("# allowzip - Sets whether to treat .zip files as .ftl files. Default: 0 (false).\n")
                cfg_file.write("# ftl_dats_path - The path to FTL's resources folder. If invalid, you'll be prompted.\n")
                cfg_file.write("# never_run_ftl - If true, there will be no offer to run FTL after patching. Default: 0 (false).\n")
                cfg_file.write("#\n")
                cfg_file.write("# highlightall - Deprecated.\n")
                cfg_file.write("# macmodsdir - Deprecated. Each OS keeps mods in GMM/mods/ now.\n")
                cfg_file.write("#\n")
                cfg_file.write("\n")
                arg_dict["config_parser"].write(cfg_file)

        all_mod_names = load_modorder()
        save_modorder(all_mod_names)

        def next_func(arg_dict):
            self.invoke_later(self.ACTION_MAIN_WINDOW_CLOSED, arg_dict)

        self._mygui.invoke_later(self._mygui.ACTION_SHOW_MAIN_WINDOW, {"mod_names":all_mod_names, "next_func":next_func})

    def _main_window_closed(self, arg_dict):
        for arg in ["all_mods", "selected_mods"]:
            if (arg not in arg_dict):
                logging.error("Missing arg %s for %s.%s()." % (arg, self.__class__.__name__, inspect.stack()[0][3]))
                return

        if (arg_dict["selected_mods"] is None):
            logging.debug("User didn't click the \"Patch\" button. Exiting.")
            self._mygui.invoke_later(self._mygui.ACTION_DIE, {})
            return

        save_modorder(arg_dict["all_mods"])

        logging.info("")
        logging.info("Patching...")
        logging.info("")

        def wrapper_finished_func(payload_result):
            self.invoke_later(self.ACTION_PATCHING_FINISHED, {"result":payload_result})

        def wrapper_exception_func(err):
            logging.exception(err)
            self.invoke_later(self.ACTION_PATCHING_FINISHED, {"result":False})

        self._patch_thread = killable_threading.WrapperThread()
        self._patch_thread.set_payload(patch_dats, arg_dict["selected_mods"])
        self._patch_thread.set_success_func(wrapper_finished_func)
        self._patch_thread.set_failure_func(wrapper_exception_func)
        self._patch_thread.start()

    def _patching_finished(self, arg_dict):
        global never_run_ftl

        for arg in ["result"]:
            if (arg not in arg_dict):
                logging.error("Missing arg %s for %s.%s()." % (arg, self.__class__.__name__, inspect.stack()[0][3]))
                return

        logging.info("")
        if (arg_dict["result"] is True):
            logging.info("Patching succeeded.")

            ftl_exe_path = None
            if (never_run_ftl is False):
                ftl_exe_path = find_ftl_exe()

            self._mygui.invoke_later(self._mygui.ACTION_PATCHING_SUCCEEDED, {"ftl_exe_path":ftl_exe_path})
        else:
            logging.info("Patching failed.")
            self._mygui.invoke_later(self._mygui.ACTION_PATCHING_FAILED, {})

        self._mygui.invoke_later(self._mygui.ACTION_DIE, {})

    def invoke_later(self, action_name, arg_dict):
        """Schedules an action to occur in this thread. (thread-safe)"""
        self._event_queue.put((action_name, arg_dict))


def main():
    global APP_NAME
    global dir_self, dir_mods, dir_res

    # dir_self was set earlier.
    dir_mods = os.path.join(dir_self, "mods")
    dir_res = None  # Set this later.

    try:
        logging.info("%s (on %s)" % (APP_NAME, platform.platform(aliased=True, terse=False)))
        logging.info("Rooting at: %s\n" % dir_self)

        # Start the GUI.
        mygui = RootWindow(None)
        mygui.withdraw()

        # Tkinter mainloop doesn't normally die and let its exceptions be caught.
        def tk_error_func(exc, val, tb):
            logging.exception("%s" % exc)
            mygui.destroy()
        mygui.report_callback_exception = tk_error_func

        logic_thread = LogicThread(mygui)
        logic_thread.start()

        try:
            mygui.mainloop()
        finally:
            mygui.done = True

    except (Exception) as err:
        logging.exception(err)


if (__name__ == "__main__"):
    main()
