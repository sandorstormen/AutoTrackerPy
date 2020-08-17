#!/usr/bin/env python
"""python-xlib example which reacts to changing the active window/title.
Requires:
- Python
- python-xlib
Tested with Python 2.x because my Kubuntu 14.04 doesn't come with python-xlib
for Python 3.x.
Design:
-------
Any modern window manager that isn't horrendously broken maintains an X11
property on the root window named _NET_ACTIVE_WINDOW.
Any modern application toolkit presents the window title via a property
named _NET_WM_NAME.
This listens for changes to both of them and then hides duplicate events
so it only reacts to title changes once.
Known Bugs:
-----------
- Under some circumstances, I observed that the first window creation and last
  window deletion on on an empty desktop (ie. not even a taskbar/panel) would
  go ignored when using this test setup:
      Xephyr :3 &
      DISPLAY=:3 openbox &
      DISPLAY=:3 python3 x11_watch_active_window.py
      # ...and then launch one or more of these in other terminals
      DISPLAY=:3 leafpad
"""
import threading
import numpy as np
import datetime
import time
from Xlib.ext import record
import copy
import os
import psutil
import pandas as pd
# pylint: disable=unused-import
from contextlib import contextmanager
from typing import Any, Dict, Optional, Tuple, Union  # noqa

from Xlib import X
from Xlib.display import Display
from Xlib.error import XError
from Xlib.xobject.drawable import Window
from Xlib.protocol.rq import Event

active_device_time = np.array(object=[], dtype=np.float32)
prev_device_time = None
prev_device_counter = None
start_device_time = None
prev_title = None
current_title = None
window_actives = {}

main_process_pid = None

# Connect to the X server and get the root window
display_focus = Display()
root = display_focus.screen().root

# Prepare the property names we use so they can be fed into X11 APIs
NET_ACTIVE_WINDOW = display_focus.intern_atom('_NET_ACTIVE_WINDOW')
NET_WM_NAME = display_focus.intern_atom('_NET_WM_NAME')  # UTF-8
WM_NAME = display_focus.intern_atom('WM_NAME')           # Legacy encoding

last_seen = {'xid': None, 'title': None}  # type: Dict[str, Any]

def print_window_actives(actives: dict):
    for title, list_times in actives.items():
        print(title)
        for (start_time, end_time) in list_times:
            print("{} - {}".format(start_time, end_time))
    print("---------------------------------------")

@contextmanager
def window_obj(win_id: Optional[int]) -> Window:
    """Simplify dealing with BadWindow (make it either valid or None)"""
    window_obj = None
    if win_id:
        try:
            window_obj = display_focus.create_resource_object('window', win_id)
        except XError:
            pass
    yield window_obj


def get_active_window() -> Tuple[Optional[int], bool]:
    """Return a (window_obj, focus_has_changed) tuple for the active window."""
    print("L88")
    response = root.get_full_property(NET_ACTIVE_WINDOW,
                                      X.AnyPropertyType)
    if not response:
        return None, False
    win_id = response.value[0]

    focus_changed = (win_id != last_seen['xid'])
    if focus_changed:
        with window_obj(last_seen['xid']) as old_win:
            if old_win:
                old_win.change_attributes(event_mask=X.NoEventMask)

        last_seen['xid'] = win_id
        with window_obj(win_id) as new_win:
            if new_win:
                new_win.change_attributes(event_mask=X.PropertyChangeMask)

    return win_id, focus_changed


def _get_window_name_inner(win_obj: Window) -> str:
    """Simplify dealing with _NET_WM_NAME (UTF-8) vs. WM_NAME (legacy)"""
    for atom in (NET_WM_NAME, WM_NAME):
        try:
            window_name = win_obj.get_full_property(atom, 0)
        except UnicodeDecodeError:  # Apparently a Debian distro package bug
            title = "<could not decode characters>"
        else:
            if window_name:
                win_name = window_name.value  # type: Union[str, bytes]
                if isinstance(win_name, bytes):
                    # Apparently COMPOUND_TEXT is so arcane that this is how
                    # tools like xprop deal with receiving it these days
                    win_name = win_name.decode('UTF-8', 'replace')
                return win_name
            else:
                title = "<unnamed window>"

    return "{} (XID: {})".format(title, win_obj.id)


def get_window_name(win_id: Optional[int]) -> Tuple[Optional[str], bool]:
    """Look up the window name for a given X11 window ID"""
    if not win_id:
        last_seen['title'] = None
        return last_seen['title'], True

    title_changed = False
    with window_obj(win_id) as wobj:
        if wobj:
            try:
                win_title = _get_window_name_inner(wobj)
            except XError:
                pass
            else:
                title_changed = (win_title != last_seen['title'])
                last_seen['title'] = win_title

    return last_seen['title'], title_changed


def handle_xevent(event: Event):
    """Handler for X events which ignores anything but focus/title change"""
    if event.type != X.PropertyNotify:
        return
    
    changed = False
    if event.atom == NET_ACTIVE_WINDOW:
        if get_active_window()[1]:
            get_window_name(last_seen['xid'])  # Rely on the side-effects
            changed = True
    elif event.atom in (NET_WM_NAME, WM_NAME):
        changed = changed or get_window_name(last_seen['xid'])[1]

    if changed:
        print("In")
        handle_change(last_seen)
        print("Out")


def handle_change(new_state: dict):
    global current_title, prev_title
    current_title = new_state["title"]
    #print("Window changed to: {}".format(current_title))
    save_thread = save_to_disk_thread(3, "SaveToDisk", 3)
    save_thread.start()
    

class window_focus_thread (threading.Thread):
    def __init__(self, threadID, name, counter):
        threading.Thread.__init__(self)
        self.threadID = threadID
        self.name = name
        self.counter = counter
        global prev_title, current_title
        # Listen for _NET_ACTIVE_WINDOW changes
        root.change_attributes(event_mask=(X.PropertyChangeMask))
    
        # Prime last_seen with whatever window was active when we started this
        get_window_name(get_active_window()[0])
        current_title = last_seen["title"]
        window_actives.update({last_seen["title"]:[]})

    def run(self):
        print("Running window focus thread")
        while True:  # next_event() sleeps until we get an event
            handle_xevent(display_focus.next_event())

def check_if_device_active(event):
    global prev_device_time, prev_device_counter, prev_title, current_title, window_actives, start_device_time
    # print(event)
    # if prev_device_counter is None:
    #     prev_device_counter = time.perf_counter()
    #     prev_device_time = datetime.datetime.now()
    #     start_device_time = prev_device_time
    #     print("prev_device_counter is None")
    if current_title != prev_title:
        #print("{} != {}".format(current_title, prev_title))
        if (time.perf_counter() - prev_device_counter) > 5:
            try:
                window_actives[prev_title].append( (start_device_time, prev_device_time+datetime.timedelta(0, 10)))
            except:
                window_actives.update({prev_title:[]})
                window_actives[prev_title].append( (start_device_time, prev_device_time+datetime.timedelta(0, 10)))
            prev_device_counter = time.perf_counter()
            prev_device_time = datetime.datetime.now()
            start_device_time = prev_device_time
        
        else:
            try:
                window_actives[prev_title].append( (start_device_time, datetime.datetime.now()))
            except:
                window_actives.update({prev_title:[]})
                window_actives[prev_title].append( (start_device_time, datetime.datetime.now()))
            prev_device_counter = time.perf_counter()
            prev_device_time = datetime.datetime.now()
            start_device_time = prev_device_time
        prev_title = current_title
    
    if current_title == prev_title:
        #print("{} == {}".format(current_title, prev_title))
        if (time.perf_counter() - prev_device_counter) > 5:
            #print("Timeout!")
            #print("{} - {} = {}".format(time.perf_counter(), prev_device_counter, (time.perf_counter() - prev_device_counter) ))
            try:
                window_actives[current_title].append( (start_device_time, prev_device_time+datetime.timedelta(0, 10)))
            except:
                window_actives.update({current_title:[]})
                window_actives[current_title].append( (start_device_time, prev_device_time+datetime.timedelta(0, 10)))
            #print_window_actives(window_actives)
            prev_device_counter = time.perf_counter()
            prev_device_time = datetime.datetime.now()
            start_device_time = prev_device_time
            #print("{} new prev_device_counter".format(prev_device_counter))
        else:
            #print("KeyPress Timer is on")
            prev_device_counter = time.perf_counter()
            prev_device_time = datetime.datetime.now()

class input_device_thread (threading.Thread):
    def __init__(self, threadID, name, counter):
        global prev_device_time, prev_device_counter, start_device_time
        threading.Thread.__init__(self)
        self.threadID = threadID
        self.name = name
        self.counter = counter
        prev_device_counter = time.perf_counter()
        prev_device_time = datetime.datetime.now()
        start_device_time = prev_device_time

    def run(self):
        global prev_device_time, prev_device_counter, start_device_time
        print("Running input thread")
        
        display_input = Display()
        context = display_input.record_create_context(0, [record.AllClients], [{
            'core_requests': (0, 0),
            'core_replies': (0, 0),
            'ext_requests': (0, 0, 0, 0),
            'ext_replies': (0, 0, 0, 0),
            'delivered_events': (0, 0),
            'device_events': (X.KeyReleaseMask, X.ButtonReleaseMask, X.MotionNotify, X.ButtonPress, X.ButtonRelease),
            'errors': (0, 0),
            'client_started': False,
            'client_died': False,
        }])
        display_input.record_enable_context(context, check_if_device_active)
        display_input.record_free_context(context)

class save_to_disk_thread (threading.Thread):
    def __init__(self, threadID, name, counter):
        threading.Thread.__init__(self)
        self.threadID = threadID
        self.name = name
        self.counter = counter
        
    def run(self):
        global window_actives
        if psutil.Process(main_process_pid).memory_percent() > 1:
            if os.path.exists("Activity.csv"):
                df = pd.read_csv("Activity.csv")
            else:
                columns = ['Title','Start', 'End']
                df = pd.DataFrame(columns=columns)
            win_act_cpy = copy.deepcopy(window_actives)
            for title, time_list in win_act_cpy.items():
                for start, end in time_list:
                    df = df.append({'Title':title, 'Start':start, 'End':end}, ignore_index=True)
            for title, time_list in win_act_cpy.items():
                time_list.clear()
            df.to_csv("Activity.csv")
        
if __name__ == '__main__':
    current_title = "None"
    main_process_pid = os.getpid()
    input_thread = input_device_thread(1, "InputThread", 1)
    focus_thread = window_focus_thread(2, "WindowFocusThread", 2)
    input_thread.start()
    focus_thread.start()

    # while True:
    #     time.sleep(10)
    #     print_window_actives(window_actives)
    
    input_thread.join()
    focus_thread.join()
