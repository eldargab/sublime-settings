from common import *
import fnmatch
import functools
import os
import re
import sublime
import sublime_plugin
import subprocess
from threading import Thread
import time

# This regex matches an unindented line, followed by zero or more
# indented, non-empty lines.
# The first line is divided into a filename, a line number, and a column.
error_output_regex = re.compile(
    r'^(\S*):(\d+):(\d+):(.*$(?:\n^[ \t].*$)*)',
    re.MULTILINE)

# Extract the filename, line, column, and description from an error message:
result_file_regex = r'^(\S*?): line (\d+), column (\d+): (.*)$'

class SublimeHaskellAutobuild(sublime_plugin.EventListener):
    def on_post_save(self, view):
        # If the edited file was Haskell code within a cabal project, try to 
        # compile it.
        cabal_project_dir = get_cabal_project_dir_of_view(view)
        if cabal_project_dir is not None:
            # On another thread, wait for the build to finish.
            write_output(view, 'Rebuilding...', cabal_project_dir)
            thread = Thread(
                target=wait_for_build_to_complete,
                args=(view, cabal_project_dir))
            thread.start()

class ErrorMessage(object):
    "Describe an error or warning message produced by GHC."
    def __init__(self, filename, line, column, message):
        self.filename = filename
        self.line = int(line)
        self.column = int(column)
        self.message = message
        self.is_warning = 'warning' in message.lower()

    def __str__(self):
        return '{0}: line {1}, column {2}: {3}'.format(
            self.filename,
            self.line,
            self.column,
            self.message)

    def find_region_in_view(self, view):
        "Return the Region referred to by this error message."
        # Convert line and column count to zero-based indices:
        point = view.text_point(self.line - 1, self.column - 1)
        # Return the whole line:
        region = view.line(point)
        region = trim_region(view, region)
        return region

def wait_for_build_to_complete(view, cabal_project_dir):
    """Start 'cabal build', wait for it to complete, then parse and diplay
    the resulting errors."""
    exit_code, stdout, stderr = call_and_wait(
        ['cabal', 'build'],
        cwd=cabal_project_dir)
    # The process has terminated; parse and display the output:
    parsed_messages = parse_error_messages(cabal_project_dir, stderr)
    error_messages = '\n'.join([str(x) for x in parsed_messages])
    success_message = 'SUCCEEDED' if exit_code == 0 else 'FAILED'
    output = '{0}\n\nBuild {1}'.format(error_messages, success_message)
    # Use set_timeout() so that the call occurs on the main Sublime thread:
    callback = functools.partial(write_output, view, output, cabal_project_dir)
    sublime.set_timeout(callback, 0)
    callback = functools.partial(mark_errors_in_views, parsed_messages)
    sublime.set_timeout(callback, 0)

def mark_errors_in_views(errors):
    "Mark the regions in open views where errors were found."
    begin_time = time.clock()
    # Mark each diagnostic in each open view in all windows:
    for w in sublime.windows():
        for v in w.views():
            view_filename = v.file_name()
            errors_in_view = filter(
                lambda x: are_paths_equal(view_filename, x.filename),
                errors)
            mark_errors_in_this_view(errors_in_view, v)
    end_time = time.clock()
    log('total time to mark {0} diagnostics: {1} seconds'.format(
        len(errors), end_time - begin_time))
    
def mark_errors_in_this_view(errors, view):
    WARNING_REGION_KEY = 'subhs-warnings'
    ERROR_REGION_KEY = 'subhs-errors'
    # Clear old regions:
    view.erase_regions(WARNING_REGION_KEY)
    view.erase_regions(ERROR_REGION_KEY)
    # Add all error and warning regions in this view.
    error_regions = []
    warning_regions = []
    for e in errors:
        region = e.find_region_in_view(view)
        if (e.is_warning):
            warning_regions.append(region)
        else:
            error_regions.append(region)
    # Mark warnings:
    view.add_regions(
        WARNING_REGION_KEY,
        warning_regions,
        'invalid.warning',
        'grey_x',
        sublime.DRAW_OUTLINED)
    # Mark errors:
    view.add_regions(
        ERROR_REGION_KEY,
        error_regions,
        'invalid',
        'grey_x',
        sublime.DRAW_OUTLINED)

def write_output(view, text, cabal_project_dir):
    "Write text to Sublime's output panel."
    PANEL_NAME = 'haskell_error_checker'
    output_view = view.window().get_output_panel(PANEL_NAME)
    output_view.set_read_only(False)
    # Configure Sublime's error message parsing:
    output_view.settings().set("result_file_regex", result_file_regex)
    output_view.settings().set("result_base_dir", cabal_project_dir)
    # Write to the output buffer:
    edit = output_view.begin_edit()
    output_view.insert(edit, 0, text)
    output_view.end_edit(edit)
    # Set the selection to the beginning of the view so that "next result" works:
    output_view.sel().clear()
    output_view.sel().add(sublime.Region(0))
    output_view.set_read_only(True)
    # Show the results panel:
    view.window().run_command('show_panel', {'panel': 'output.' + PANEL_NAME})

def parse_error_messages(base_dir, text):
    "Parse text into a list of ErrorMessage objects."
    matches = error_output_regex.finditer(text)
    messages = []
    for m in matches:
        filename, line, column, messy_details = m.groups()
        messages.append(ErrorMessage(
            # Record the absolute, normalized path.
            os.path.normpath(os.path.join(base_dir, filename)),
            line,
            column,
            clean_whitespace(messy_details)))
    return messages

def trim_region(view, region):
    "Return the specified Region, but without leading or trailing whitespace."
    text = view.substr(region)
    # Regions may be selected backwards, so b could be less than a.
    a = min(region.a, region.b)
    b = max(region.a, region.b)
    # Figure out how much to move the endpoints to lose the space.
    # If the region is entirely whitespace, give up and return it unchanged.
    if text.isspace():
        return region
    else:
        text_trimmed_on_left = text.lstrip()
        text_trimmed = text_trimmed_on_left.rstrip()
        a += len(text) - len(text_trimmed_on_left)
        b -= len(text_trimmed_on_left) - len(text_trimmed)
        return sublime.Region(a, b)

def clean_whitespace(text):
    """Remove leading and trailing whitespace, plus replaces any interior 
    whitespace with a single space."""
    text = text.strip()
    text = re.sub('\s+', ' ', text)
    return text
