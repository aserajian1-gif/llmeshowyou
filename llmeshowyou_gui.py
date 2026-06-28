#!/usr/bin/env python3
"""
llmeshowyou_gui — Windows GUI for llmeshowyou multi-language file mapper.
Opens a tkinter window with full CLI parity plus clickable opencode launch,
cross-file import graph, god nodes, wiki export, cost tracker, watch mode,
and git hook management.

Usage:
  python llmeshowyou_gui.py
"""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from llmeshowyou import (
        analyze_file,
        render_map,
        write_map,
        _calc_savings as _engine_calc_savings,
        map_path_for,
        read_source,
        find_source_files,
        is_stale,
        is_major_change,
        sha256_file,
        count_lines,
        FileMap,
        ProjectGraph,
        build_project_graph,
        rank_god_nodes,
        render_html_index,
        render_wiki,
        export_graphml,
        export_neo4j_cypher,
        query_graph,
        shortest_path,
        explain_node,
        load_cost,
        fmt_tokens,
        CostTracker,
        install_hook,
        uninstall_hook,
        status_hook,
        watch_files,
        LANGUAGE_EXTENSIONS,
        SUPPORTED_EXTENSIONS,
    )
    ENGINE_OK = True
except ImportError as e:
    ENGINE_OK = False
    ENGINE_ERR = str(e)

CONFIG_FILE = Path.home() / '.llmeshowyou_gui.json'
MAX_RECENT = 10

_COLORS = {
    'bg': '#f0f0f0',
    'fresh': '#2e7d32',
    'stale': '#e65100',
    'mapped': '#1565c0',
    'error': '#c62828',
    'missing': '#6a1b9a',
    'link': '#1565c0',
    'grey': '#757575',
    'god': '#8e24aa',
}


class LLMEShowYouGUI:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title('llmeshowyou — Multi-Language File Mapper')
        self.root.geometry('1200x860')
        self.root.minsize(1000, 700)
        if hasattr(self.root, 'tk.call'):
            try:
                self.root.tk.call('encoding', 'system', 'utf-8')
            except Exception:
                pass

        self.source_file = tk.StringVar()
        self.source_folder = tk.StringVar()
        self.output_dir = tk.StringVar()
        self.min_lines = tk.IntVar(value=0)
        self.recursive = tk.BooleanVar(value=False)
        self.combined = tk.BooleanVar(value=False)
        self.force = tk.BooleanVar(value=False)
        self.major_only = tk.BooleanVar(value=False)
        self.all_langs = tk.BooleanVar(value=False)
        self.watch_mode = tk.BooleanVar(value=False)
        self.include_ac_dod = tk.BooleanVar(value=True)
        self.discipline_path = tk.StringVar(value='')
        self.deepseek_key = tk.StringVar(value='')
        self.recent_files: list[str] = []

        self._cancel_flag = False
        self._running = False
        self._watching = False
        self._q: queue.Queue = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._watch_thread: Optional[threading.Thread] = None
        self._results: list[dict] = []
        self._result_id_map: dict[Path, str] = {}
        self._all_maps: list[FileMap] = []

        if not ENGINE_OK:
            messagebox.showerror('Engine Error',
                                  f'llmeshowyou engine not found:\n{ENGINE_ERR}\n\n'
                                  f'Make sure llmeshowyou.py is in the same folder.')
            sys.exit(1)

        self._load_config()
        self._build_ui()
        self._bind_events()
        self._poll_queue()
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        self.root.after(200, self._ensure_deepseek_key)
        self.root.mainloop()

    # ##[Configuration Persistence]

    def _load_config(self) -> None:
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
            self.recent_files = data.get('recent', [])
            self.source_folder.set(data.get('folder', ''))
            self.output_dir.set(data.get('outdir', ''))
            self.min_lines.set(data.get('min_lines', 0))
            self.recursive.set(data.get('recursive', False))
            self.combined.set(data.get('combined', False))
            self.all_langs.set(data.get('all_langs', False))
            self.watch_mode.set(data.get('watch_mode', False))
            self.discipline_path.set(data.get('discipline_path', ''))
            self.deepseek_key.set(data.get('deepseek_key', ''))
        except Exception:
            self.recent_files = []
        # Env var wins if set; otherwise seed env from stored key.
        env_key = os.environ.get('DEEPSEEK_API_KEY', '')
        if env_key:
            self.deepseek_key.set(env_key)
        elif self.deepseek_key.get():
            os.environ['DEEPSEEK_API_KEY'] = self.deepseek_key.get()

    def _save_config(self) -> None:
        try:
            data = {
                'recent': self.recent_files[:MAX_RECENT],
                'folder': self.source_folder.get(),
                'outdir': self.output_dir.get(),
                'min_lines': self.min_lines.get(),
                'recursive': self.recursive.get(),
                'combined': self.combined.get(),
                'all_langs': self.all_langs.get(),
                'watch_mode': self.watch_mode.get(),
                'discipline_path': self.discipline_path.get(),
                'deepseek_key': self.deepseek_key.get(),
            }
            CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding='utf-8')
            # Best-effort: restrict perms since the file now holds a secret.
            try:
                os.chmod(CONFIG_FILE, 0o600)
            except Exception:
                pass
        except Exception:
            pass

    def _add_recent(self, path: str) -> None:
        if path in self.recent_files:
            self.recent_files.remove(path)
        self.recent_files.insert(0, path)
        self.recent_files = self.recent_files[:MAX_RECENT]
        self._rebuild_recent_menu()
        self._save_config()

    # ##[DeepSeek API Key]

    @staticmethod
    def _mask_key(key: str) -> str:
        if not key:
            return '(not set)'
        if len(key) <= 8:
            return '*' * len(key)
        return f'{key[:4]}\u2026{key[-4:]} ({len(key)} chars)'

    def _prompt_deepseek_key(self, force: bool = False) -> bool:
        """Ask the user for the DeepSeek API key. Persists + exports to env.

        Returns True if a key is set afterwards. When force=False and a key is
        already present, the dialog is pre-filled so the user can keep it.
        """
        from tkinter import simpledialog
        current = self.deepseek_key.get()
        prompt = ('Enter your DeepSeek API key.\n'
                  'Get one at https://platform.deepseek.com/api_keys\n\n'
                  f'Current: {self._mask_key(current)}\n'
                  '(Leave blank to cancel; stored locally in '
                  f'{CONFIG_FILE.name})')
        val = simpledialog.askstring('DeepSeek API Key', prompt,
                                     parent=self.root, show='*',
                                     initialvalue='' if force else current)
        if val is None or not val.strip():
            return bool(self.deepseek_key.get())
        key = val.strip()
        self.deepseek_key.set(key)
        os.environ['DEEPSEEK_API_KEY'] = key
        self._save_config()
        self._status(f'DeepSeek API key set: {self._mask_key(key)}')
        return True

    def _clear_deepseek_key(self) -> None:
        from tkinter import messagebox
        if not messagebox.askyesno('Clear DeepSeek API Key',
                                   'Remove the stored DeepSeek API key?'):
            return
        self.deepseek_key.set('')
        os.environ.pop('DEEPSEEK_API_KEY', None)
        self._save_config()
        self._status('DeepSeek API key cleared.')

    def _ensure_deepseek_key(self) -> None:
        """Prompt once on startup if no key is available from env or config."""
        if self.deepseek_key.get() or os.environ.get('DEEPSEEK_API_KEY'):
            return
        from tkinter import messagebox
        if messagebox.askyesno(
                'DeepSeek API Key',
                'No DeepSeek API key found.\n\n'
                'The SAW/OpenCode harness and DeepSeek-backed launches need one.\n'
                'Set it now?'):
            self._prompt_deepseek_key(force=True)

    def _setup_styles(self) -> None:
        self.root.configure(bg='#f0f0f0')
        style = ttk.Style()

        default_font = ('Segoe UI', 9)
        bold_font = ('Segoe UI', 9, 'bold')

        style.configure('.', font=default_font)

        style.configure('TButton', padding=(8, 4))
        style.configure('Primary.TButton', padding=(8, 4), font=bold_font)

        style.configure('TLabelframe.Label', font=bold_font, foreground='#444')

        style.configure('TEntry', padding=(4, 3))

        style.configure('TSpinbox', padding=(4, 3))

        style.configure('Treeview', rowheight=26, font=default_font,
                        background='white')
        style.configure('Treeview.Heading', font=bold_font, padding=(4, 4))
        style.map('Treeview',
                  background=[('selected', '#0078D4')],
                  foreground=[('selected', 'white')])

        style.configure('Horizontal.TProgressbar', thickness=8,
                        troughcolor='#e0e0e0', background='#0078D4')

    # ##[UI Building]

    def _build_ui(self) -> None:
        self._setup_styles()
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(4, weight=1)
        self.root.rowconfigure(5, weight=0)

        self._build_menu()
        self._build_top_frame()
        self._build_options_frame()
        self._build_action_bar()
        self._build_results_area()
        self._build_result_buttons()
        self._build_graph_panel()
        self._build_log_area()
        self._build_status_bar()

    def _build_menu(self) -> None:
        mb = tk.Menu(self.root, tearoff=False)
        self.root.config(menu=mb)

        file_m = tk.Menu(mb, tearoff=False)
        mb.add_cascade(label='File', menu=file_m)
        file_m.add_command(label='Open Source File...', command=self._browse_file,
                           accelerator='Ctrl+O')
        file_m.add_command(label='Open Folder...', command=self._browse_folder,
                           accelerator='Ctrl+Shift+O')
        file_m.add_separator()
        self._recent_menu = tk.Menu(file_m, tearoff=False)
        file_m.add_cascade(label='Recent Files', menu=self._recent_menu)
        self._rebuild_recent_menu()
        file_m.add_separator()
        file_m.add_command(label='Open Map Output Dir...', command=self._browse_outdir)
        file_m.add_separator()
        file_m.add_command(label='Exit', command=self._on_close, accelerator='Alt+F4')

        tools_m = tk.Menu(mb, tearoff=False)
        mb.add_cascade(label='Tools', menu=tools_m)
        tools_m.add_command(label='Map File', command=self._cmd_map_file, accelerator='F5')
        tools_m.add_command(label='Map Folder', command=self._cmd_map_folder, accelerator='F6')
        tools_m.add_separator()
        tools_m.add_command(label='Status', command=self._cmd_status, accelerator='F7')
        tools_m.add_command(label='Update', command=self._cmd_update, accelerator='F8')
        tools_m.add_command(label='Update --check', command=self._cmd_update_check, accelerator='F9')
        tools_m.add_separator()
        tools_m.add_command(label='Build Graph', command=self._cmd_graph)
        tools_m.add_command(label='Show Cost Tracker', command=self._cmd_cost)
        tools_m.add_command(label='Export Wiki', command=self._cmd_wiki)
        tools_m.add_separator()
        tools_m.add_command(label='Install Git Hook', command=lambda: self._cmd_hook('install'))
        tools_m.add_command(label='Uninstall Git Hook', command=lambda: self._cmd_hook('uninstall'))
        tools_m.add_command(label='Hook Status', command=lambda: self._cmd_hook('status'))
        tools_m.add_separator()
        tools_m.add_command(label='Open .map.md in Editor',
                            command=self._open_selected_map)
        tools_m.add_command(label='Open INDEX.map.md',
                            command=self._open_index_map)
        tools_m.add_command(label='Open in OpenCode',
                            command=self._launch_opencode_selected)
        tools_m.add_command(label='Open in Claude Code',
                            command=self._launch_claude_code_selected)
        tools_m.add_command(label='Copy opencode Command',
                            command=self._copy_opencode_cmd_selected)
        tools_m.add_command(label='Copy claude Command',
                            command=self._copy_claude_code_cmd_selected)

        settings_m = tk.Menu(mb, tearoff=False)
        mb.add_cascade(label='Settings', menu=settings_m)
        settings_m.add_command(label='Set DeepSeek API Key\u2026',
                               command=self._prompt_deepseek_key)
        settings_m.add_command(label='Clear DeepSeek API Key',
                               command=self._clear_deepseek_key)

        help_m = tk.Menu(mb, tearoff=False)
        mb.add_cascade(label='Help', menu=help_m)
        help_m.add_command(label='How to Use\u2026', command=self._show_help, accelerator='F1')
        help_m.add_separator()
        help_m.add_command(label='About', command=self._show_about)

    def _rebuild_recent_menu(self) -> None:
        self._recent_menu.delete(0, 'end')
        if not self.recent_files:
            self._recent_menu.add_command(label='(no recent files)', state='disabled')
        else:
            for f in self.recent_files:
                p = Path(f)
                label = f"{p.parent.name}/{p.name}" if p.parent.name != p.parent.drive.strip('\\') else p.name
                self._recent_menu.add_command(
                    label=label,
                    command=lambda path=f: self._open_recent(path),
                )

    def _open_recent(self, path: str) -> None:
        p = Path(path)
        if p.is_file():
            self.source_file.set(str(p.resolve()))
            self._status(f"Selected: {p.name}")
        else:
            self.recent_files.remove(path)
            self._rebuild_recent_menu()
            self._save_config()
            self._status(f"File not found, removed from recent: {path}")

    def _build_top_frame(self) -> None:
        f = ttk.LabelFrame(self.root, text='Target', padding=6)
        f.grid(row=0, column=0, sticky='nsew', padx=6, pady=(6, 2))
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text='File:').grid(row=0, column=0, sticky='w')
        e = ttk.Entry(f, textvariable=self.source_file)
        e.grid(row=0, column=1, sticky='ew', padx=(4, 4))
        ttk.Button(f, text='Browse\u2026', command=self._browse_file, width=10
                   ).grid(row=0, column=2, sticky='e')
        ttk.Button(f, text='\u2715', command=lambda: self.source_file.set(''), width=3
                   ).grid(row=0, column=3, sticky='e', padx=(0, 4))

        ttk.Label(f, text='Folder:').grid(row=1, column=0, sticky='w', pady=(3, 0))
        ttk.Entry(f, textvariable=self.source_folder
                  ).grid(row=1, column=1, sticky='ew', padx=(4, 4), pady=(3, 0))
        ttk.Button(f, text='Browse\u2026', command=self._browse_folder, width=10
                   ).grid(row=1, column=2, sticky='e', pady=(3, 0))
        ttk.Button(f, text='\u2715', command=lambda: self.source_folder.set(''), width=3
                   ).grid(row=1, column=3, sticky='e', padx=(0, 4), pady=(3, 0))

        ttk.Label(f, text='Output Dir:').grid(row=2, column=0, sticky='w', pady=(3, 0))
        o = ttk.Entry(f, textvariable=self.output_dir)
        o.grid(row=2, column=1, sticky='ew', padx=(4, 4), pady=(3, 0))
        ttk.Button(f, text='Browse\u2026', command=self._browse_outdir, width=10
                   ).grid(row=2, column=2, sticky='e', pady=(3, 0))
        ttk.Button(f, text='\u2715', command=lambda: self.output_dir.set(''), width=3
                   ).grid(row=2, column=3, sticky='e', padx=(0, 4), pady=(3, 0))

    def _build_options_frame(self) -> None:
        f = ttk.LabelFrame(self.root, text='Options', padding=6)
        f.grid(row=1, column=0, sticky='nsew', padx=6, pady=2)
        f.columnconfigure(6, weight=1)

        ttk.Checkbutton(f, text='Recursive', variable=self.recursive).grid(row=0, column=0, sticky='w', padx=(0, 8))
        ttk.Checkbutton(f, text='Combined Index', variable=self.combined).grid(row=0, column=1, sticky='w', padx=(0, 8))
        ttk.Checkbutton(f, text='Force Remap', variable=self.force).grid(row=0, column=2, sticky='w', padx=(0, 8))
        ttk.Checkbutton(f, text='Major Only', variable=self.major_only).grid(row=0, column=3, sticky='w', padx=(0, 8))
        ttk.Checkbutton(f, text='All Languages', variable=self.all_langs).grid(row=0, column=4, sticky='w', padx=(0, 8))
        ttk.Checkbutton(f, text='Watch Mode', variable=self.watch_mode).grid(row=0, column=5, sticky='w', padx=(0, 8))
        ttk.Checkbutton(f, text='AC/DoD', variable=self.include_ac_dod).grid(row=1, column=0, sticky='w', padx=(0, 8))

        ttk.Label(f, text='Min lines:').grid(row=0, column=6, sticky='e', padx=(8, 4))
        s = ttk.Spinbox(f, from_=0, to=9999, width=6, textvariable=self.min_lines)
        s.grid(row=0, column=7, sticky='w')

    def _build_action_bar(self) -> None:
        f = ttk.Frame(self.root, padding=6)
        f.grid(row=2, column=0, sticky='nsew', padx=6, pady=(2, 4))

        ttk.Button(f, text='Map File', command=self._cmd_map_file, width=12).pack(side='left', padx=(0, 3))
        ttk.Button(f, text='Map Folder', command=self._cmd_map_folder, width=12).pack(side='left', padx=(0, 3))
        ttk.Button(f, text='Status', command=self._cmd_status, width=10).pack(side='left', padx=(0, 3))
        ttk.Button(f, text='Update', command=self._cmd_update, width=10).pack(side='left', padx=(0, 3))
        ttk.Button(f, text='Update \u2013check', command=self._cmd_update_check, width=14).pack(side='left', padx=(0, 3))
        ttk.Button(f, text='Graph', command=self._cmd_graph, width=10).pack(side='left', padx=(0, 3))
        ttk.Button(f, text='Cost', command=self._cmd_cost, width=8).pack(side='left', padx=(0, 3))
        ttk.Button(f, text='Discipline', command=self.open_discipline_panel, width=11).pack(side='left', padx=(0, 3))

        ttk.Separator(f, orient='vertical').pack(side='right', fill='y', padx=6)
        ttk.Button(f, text='Help', command=self._show_help, width=8).pack(side='right', padx=(0, 3))
        ttk.Button(f, text='Cancel', command=self._cancel, width=8).pack(side='right', padx=(3, 0))

    def _build_results_area(self) -> None:
        pf = ttk.PanedWindow(self.root, orient='vertical')
        pf.grid(row=4, column=0, sticky='nsew', padx=6, pady=2)

        top_f = ttk.LabelFrame(pf, text='Results')
        bottom_f = ttk.LabelFrame(pf, text='Log')

        cols = ('file', 'lines', 'sym', 'saved', 'pct', 'status', 'mapfile')
        self._tree = ttk.Treeview(top_f, columns=cols[1:], show='tree headings',
                                   selectmode='extended')
        self._tree.heading('#0', text='File')
        self._tree.heading('lines', text='Lines')
        self._tree.heading('sym', text='Sym')
        self._tree.heading('saved', text='Saved')
        self._tree.heading('pct', text='%')
        self._tree.heading('status', text='Status')
        self._tree.heading('mapfile', text='Map')
        self._tree.column('#0', width=290, minwidth=150, stretch=True)
        self._tree.column('lines', width=50, anchor='e', stretch=False)
        self._tree.column('sym', width=45, anchor='e', stretch=False)
        self._tree.column('saved', width=65, anchor='e', stretch=False)
        self._tree.column('pct', width=50, anchor='e', stretch=False)
        self._tree.column('status', width=80, anchor='center', stretch=False)
        self._tree.column('mapfile', width=160, stretch=False)

        self._tree.tag_configure('fresh', foreground=_COLORS['fresh'])
        self._tree.tag_configure('stale', foreground=_COLORS['stale'])
        self._tree.tag_configure('mapped', foreground=_COLORS['mapped'])
        self._tree.tag_configure('missing', foreground=_COLORS['missing'])
        self._tree.tag_configure('error', foreground=_COLORS['error'])
        self._tree.tag_configure('evenrow', background='#f5f5f5')
        self._tree.tag_configure('oddrow', background='#ffffff')

        vsb = ttk.Scrollbar(top_f, orient='vertical', command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        self._tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        top_f.columnconfigure(0, weight=1)
        top_f.rowconfigure(0, weight=1)

        self._log = ScrolledText(bottom_f, height=8, wrap='word', state='disabled',
                                  font=('Consolas', 9), bg='#1e1e1e', fg='#d4d4d4',
                                  insertbackground='white')
        self._log.pack(fill='both', expand=True)
        self._log.tag_config('info', foreground='#9cdcfe')
        self._log.tag_config('ok', foreground='#4ec9b0')
        self._log.tag_config('warn', foreground='#ce9178')
        self._log.tag_config('err', foreground='#f44747')
        self._log.tag_config('bold', font=('Consolas', 9, 'bold'))
        self._log.tag_config('god', foreground='#ce93d8')

        pf.add(top_f, weight=3)
        pf.add(bottom_f, weight=1)

    def _build_result_buttons(self) -> None:
        f = ttk.Frame(self.root, padding=4)
        f.grid(row=3, column=0, sticky='nsew', padx=6, pady=(0, 2))

        ttk.Button(f, text='Open .map.md', command=self._open_selected_map, width=14
                   ).pack(side='left', padx=(0, 3))
        ttk.Button(f, text='Open in OpenCode', command=self._launch_opencode_selected, width=18
                   ).pack(side='left', padx=(0, 3))
        ttk.Button(f, text='Open in Claude Code', command=self._launch_claude_code_selected, width=19
                   ).pack(side='left', padx=(0, 3))
        ttk.Button(f, text='Copy opencode Cmd', command=self._copy_opencode_cmd_selected, width=20
                   ).pack(side='left', padx=(0, 3))
        ttk.Button(f, text='Copy claude Cmd', command=self._copy_claude_code_cmd_selected, width=20
                   ).pack(side='left', padx=(0, 3))
        ttk.Button(f, text='Export Wiki', command=self._cmd_wiki, width=14
                   ).pack(side='left', padx=(0, 3))
        ttk.Button(f, text='Clear Results', command=self._clear_results, width=14
                   ).pack(side='right', padx=(3, 0))

    def _build_graph_panel(self) -> None:
        pass

    def _build_log_area(self) -> None:
        pass

    def _build_status_bar(self) -> None:
        self._status_var = tk.StringVar(value='Ready')
        bar = ttk.Label(self.root, textvariable=self._status_var, relief='sunken',
                         anchor='w', padding=(4, 2))
        bar.grid(row=5, column=0, sticky='ew', padx=6, pady=(2, 4))
        self._progress = ttk.Progressbar(self.root, mode='indeterminate', length=120)
        self._progress.grid(row=5, column=0, sticky='e', padx=(0, 8), pady=(2, 4))

    # ##[Events]

    def _bind_events(self) -> None:
        self.root.bind('<Control-o>', lambda e: self._browse_file())
        self.root.bind('<Control-O>', lambda e: self._browse_file())
        self.root.bind('<Control-Shift-O>', lambda e: self._browse_folder())
        self.root.bind('<F1>', lambda e: self._show_help())
        self.root.bind('<F5>', lambda e: self._cmd_map_file())
        self.root.bind('<F6>', lambda e: self._cmd_map_folder())
        self.root.bind('<F7>', lambda e: self._cmd_status())
        self.root.bind('<F8>', lambda e: self._cmd_update())
        self.root.bind('<F9>', lambda e: self._cmd_update_check())
        self._tree.bind('<Double-1>', self._on_tree_double_click)
        self._tree.bind('<Button-3>', self._on_tree_right_click)

    def _on_tree_double_click(self, event=None) -> None:
        sel = self._tree.selection()
        if sel:
            items = self._get_selected_items()
            valid = [item for item in items
                     if item['status'] in ('mapped', 'fresh', 'stale')]
            if valid:
                self._launch_opencode(valid)

    def _on_tree_right_click(self, event) -> None:
        iid = self._tree.identify_row(event.y)
        if iid:
            sel = self._tree.selection()
            if iid not in sel:
                self._tree.selection_set(iid)
            menu = tk.Menu(self.root, tearoff=False)
            n = len(self._tree.selection())
            label_single = lambda s: s
            label_multi = lambda s: f"{s} ({n} files)" if n > 1 else s
            menu.add_command(label=label_multi('Open .map.md'), command=self._open_selected_map)
            menu.add_command(label=label_multi('Open in OpenCode'), command=self._launch_opencode_selected)
            menu.add_command(label=label_multi('Open in Claude Code'), command=self._launch_claude_code_selected)
            menu.add_command(label=label_multi('Copy opencode Command'), command=self._copy_opencode_cmd_selected)
            menu.add_command(label=label_multi('Copy claude Command'), command=self._copy_claude_code_cmd_selected)
            menu.add_separator()
            menu.add_command(label=label_multi('Reveal in Explorer'), command=self._reveal_selected)
            if n == 1:
                menu.add_separator()
                menu.add_command(label='Explain in Graph', command=self._explain_selected)
                menu.add_command(label='Find Shortest Path\u2026', command=self._path_selected)
            menu.tk_popup(event.x_root, event.y_root)

    def _explain_selected(self) -> None:
        sel = self._get_selected_item()
        if not sel:
            return
        folder = Path(self.source_folder.get().strip() or '.')
        from llmeshowyou import _load_graph, explain_node
        g = _load_graph(folder)
        if g is None:
            self._log_msg('No graph found. Run Graph first.', 'err')
            return
        clean = re.sub(r'\s*\[.*?\]\s*$', '', sel['name'])
        result = explain_node(g, clean)
        self._log_msg(f"Explain: {clean}", 'god')
        for line in result.split('\n'):
            if line.strip():
                self._log_msg(f"  {line.strip()}", 'info')

    def _path_selected(self) -> None:
        folder = Path(self.source_folder.get().strip() or '.')
        from llmeshowyou import _load_graph, shortest_path
        from tkinter.simpledialog import askstring
        target = askstring('Shortest Path', 'Target file name:')
        if not target:
            return
        g = _load_graph(folder)
        if g is None:
            self._log_msg('No graph found. Run Graph first.', 'err')
            return
        sel_item = self._get_selected_item()
        if not sel_item:
            return
        result = shortest_path(g, sel_item['name'], target)
        self._log_msg(result, 'god')

    # ##[Status & Log Helpers]

    def _status(self, msg: str) -> None:
        self._status_var.set(msg)
        self.root.update_idletasks()

    def _log_msg(self, text: str, tag: str = 'info') -> None:
        self._q.put(('log', text, tag))

    def _do_log(self, text: str, tag: str = 'info') -> None:
        self._log.config(state='normal')
        ts = datetime.now().strftime('%H:%M:%S')
        self._log.insert('end', f'[{ts}] ', 'bold')
        self._log.insert('end', text + '\n', tag)
        self._log.see('end')
        self._log.config(state='disabled')

    def _busy(self, busy: bool) -> None:
        self._running = busy
        if busy:
            self._progress.start(15)
            self._status('Working\u2026')
        else:
            self._progress.stop()
            self._status('Ready')

    def _cancel(self) -> None:
        if self._running:
            self._cancel_flag = True
            self._log_msg('Cancel requested\u2026', 'warn')

    # ##[File Dialogs]

    def _browse_file(self) -> None:
        exts = [('Python Files', '*.py'), ('All Supported', '*.py *.js *.ts *.go *.rs *.java *.c *.cpp *.rb *.cs *.kt *.swift *.php'), ('All Files', '*.*')]
        path = filedialog.askopenfilename(
            title='Select Source File',
            filetypes=exts,
            initialdir=self.source_folder.get() or None,
        )
        if path:
            self.source_file.set(path)
            self._add_recent(path)
            self._status(f"Selected: {Path(path).name}")

    def _browse_folder(self) -> None:
        path = filedialog.askdirectory(
            title='Select Folder',
            initialdir=self.source_folder.get() or None,
        )
        if path:
            self.source_folder.set(path)
            self._status(f"Folder: {Path(path).name}")

    def _browse_outdir(self) -> None:
        path = filedialog.askdirectory(
            title='Select Output Directory for Maps',
            initialdir=self.output_dir.get() or None,
        )
        if path:
            self.output_dir.set(path)

    # ##[Background Worker]

    def _start_worker(self, target, args=None) -> None:
        if self._running:
            messagebox.showinfo('Busy', 'An operation is already running.')
            return
        self._cancel_flag = False
        self._all_maps.clear()
        self._busy(True)
        self._worker = threading.Thread(
            target=target,
            args=args or (),
            daemon=True,
        )
        self._worker.start()

    def _poll_queue(self) -> None:
        while True:
            try:
                msg = self._q.get_nowait()
            except queue.Empty:
                break
            kind = msg[0]
            if kind == 'log':
                self._do_log(msg[1], msg[2])
            elif kind == 'result':
                self._add_result_row(*msg[1:])
            elif kind == 'done':
                self._on_worker_done(msg[1] if len(msg) > 1 else '')
            elif kind == 'progress':
                self._status(msg[1])
            elif kind == 'clear_results':
                self._clear_results()
            elif kind == 'combined':
                self._write_combined(msg[1])
            elif kind == 'graph_result':
                self._show_graph_summary(msg[1])
            elif kind == 'cost_result':
                self._show_cost_summary(msg[1])
            elif kind == 'hook_result':
                self._log_msg(msg[1], msg[2] if len(msg) > 2 else 'ok')
        self.root.after(80, self._poll_queue)

    @staticmethod
    def _fmt_savings(tokens: int) -> str:
        if tokens >= 1000:
            return f"{tokens / 1000:.1f}K" if tokens < 100000 else f"{tokens // 1000}K"
        return str(tokens)

    def _calc_savings(self, src: Path, fm: FileMap) -> int:
        # Delegate to the engine so the GUI and CLI report identical numbers.
        return _engine_calc_savings(fm)

    def _add_result_row(self, src: Path, fm: Optional[FileMap], status: str,
                        err_msg: str = '', savings: int = 0) -> None:
        name = src.name
        lines = fm.lines if fm else 0
        syms = fm.total_symbols if fm else 0
        map_path = map_path_for(src)
        map_exists = map_path.exists() and status != 'error'

        if self.output_dir.get():
            map_path = Path(self.output_dir.get()) / map_path.name

        tag = status if status in ('fresh', 'stale', 'mapped', 'missing', 'error') else 'info'

        saved_str = self._fmt_savings(savings) if savings else '\u2014'
        if savings and fm:
            src_tok = max(1, int(fm.size_bytes / 3.5))
            pct = round(savings / src_tok * 100)
            pct_str = f"~{pct}%"
        else:
            pct_str = '\u2014'

        map_label = str(map_path) if map_exists else ''
        if status == 'mapped' and map_exists:
            map_label = '\u2713 ' + map_path.name
        elif status == 'fresh':
            map_label = '\u2713 fresh'
        elif status == 'stale':
            map_label = '\u26a0 stale'
        elif status == 'missing':
            map_label = '\u2014 missing'

        lang_info = f" [{fm.language}]" if fm and fm.language != 'python' else ''
        row_idx = len(self._results)
        row_tag = 'evenrow' if row_idx % 2 == 0 else 'oddrow'
        iid = self._tree.insert('', 'end', text=name + lang_info,
                                 values=(str(lines), str(syms), saved_str, pct_str, status, map_label),
                                 tags=(tag, row_tag))
        self._result_id_map[src.resolve()] = iid

    def _on_worker_done(self, extra: str = '') -> None:
        self._busy(False)
        summary = f"Done. {extra}" if extra else 'Done.'
        self._status(summary)
        self._log_msg(summary, 'ok')
        if self._cancel_flag:
            self._log_msg('Cancelled by user.', 'warn')
            self._cancel_flag = False

    def _clear_results(self) -> None:
        for item in self._tree.get_children():
            self._tree.delete(item)
        self._result_id_map.clear()
        self._results.clear()
        self._all_maps.clear()

    def _on_close(self) -> None:
        self._save_config()
        self.root.destroy()

    # ##[Worker: Map File]

    def _cmd_map_file(self) -> None:
        path = self.source_file.get().strip()
        if not path:
            messagebox.showinfo('No File', 'Select a source file first.')
            return
        src = Path(path)
        if not src.is_file():
            messagebox.showerror('Not Found', f'File not found:\n{src}')
            return
        self._start_worker(self._do_map_file, (src,))

    def _do_map_file(self, src: Path) -> None:
        min_l = self.min_lines.get()
        if min_l and count_lines(src) < min_l:
            self._q.put(('log', f"Skipped {src.name} (under {min_l} lines)", 'warn'))
            self._q.put(('done', ''))
            return

        try:
            text, lines = read_source(src)
            fm = analyze_file(src, text, lines)
            out = self._get_outpath(src)
            force = self.force.get()

            if not force and out.exists():
                stored = self._read_stored_hash(out)
                if stored == fm.sha256:
                    savings = self._calc_savings(src, fm)
                    self._q.put(('result', src, fm, 'fresh', '', savings))
                    self._q.put(('log', f"  fresh  {src.name} ({fm.lines} lines, lang={fm.language}, saves {LLMEShowYouGUI._fmt_savings(savings)} tok)", 'ok'))
                    self._q.put(('done', '1 file fresh'))
                    return

            write_map(fm, out)
            savings = self._calc_savings(src, fm)
            self._q.put(('result', src, fm, 'mapped', '', savings))
            self._q.put(('log', f"  mapped {src.name} ({fm.lines} lines, {fm.total_symbols} sym, lang={fm.language}, saves ~{LLMEShowYouGUI._fmt_savings(savings)} tok)", 'ok'))
            self._all_maps.append(fm)
            self._q.put(('done', '1 file mapped'))
        except SyntaxError as e:
            self._q.put(('result', src, None, 'error'))
            self._q.put(('log', f"  syntax error {src.name}: {e}", 'err'))
            self._q.put(('done', '1 error'))
        except Exception as e:
            self._q.put(('result', src, None, 'error'))
            self._q.put(('log', f"  error {src.name}: {e}", 'err'))
            self._q.put(('done', '1 error'))

        if self.combined.get() and self._all_maps:
            self._write_combined_internal()

    # ##[Worker: Map Folder]

    def _cmd_map_folder(self) -> None:
        folder = self.source_folder.get().strip()
        if not folder:
            messagebox.showinfo('No Folder', 'Select a folder first.')
            return
        if not Path(folder).is_dir():
            messagebox.showerror('Not Found', f'Folder not found:\n{folder}')
            return
        self._start_worker(self._do_map_folder, (folder,))

    def _do_map_folder(self, folder: str) -> None:
        rec = self.recursive.get()
        all_langs = self.all_langs.get()
        ext = None if not all_langs else set(SUPPORTED_EXTENSIONS)
        files = find_source_files([folder], recursive=rec, extra_exts=ext, gitignore_root=Path(folder))
        min_l = self.min_lines.get()
        force = self.force.get()
        counted = 0
        errors = 0
        skipped = 0

        self._q.put(('clear_results',))
        self._q.put(('progress', f"Scanning {len(files)} files\u2026"))

        for src in files:
            if self._cancel_flag:
                break
            if min_l and count_lines(src) < min_l:
                skipped += 1
                continue
            try:
                text, lines = read_source(src)
                fm = analyze_file(src, text, lines)
                out = self._get_outpath(src)

                if not force and out.exists():
                    stored = self._read_stored_hash(out)
                    if stored == fm.sha256:
                        savings = self._calc_savings(src, fm)
                        self._q.put(('result', src, fm, 'fresh', '', savings))
                        self._q.put(('log', f"  fresh  {src.name} ({fm.lines} l, {fm.total_symbols} sym, lang={fm.language}, saves ~{LLMEShowYouGUI._fmt_savings(savings)} tok)", 'ok'))
                        counted += 1
                        continue

                write_map(fm, out)
                savings = self._calc_savings(src, fm)
                self._q.put(('result', src, fm, 'mapped', '', savings))
                self._q.put(('log', f"  mapped {src.name} ({fm.lines} l, {fm.total_symbols} sym, lang={fm.language}, saves ~{LLMEShowYouGUI._fmt_savings(savings)} tok)", 'ok'))
                self._all_maps.append(fm)
                counted += 1
            except SyntaxError as e:
                self._q.put(('result', src, None, 'error'))
                self._q.put(('log', f"  syntax error {src.name}: {e}", 'err'))
                errors += 1
            except Exception as e:
                self._q.put(('result', src, None, 'error'))
                self._q.put(('log', f"  error {src.name}: {e}", 'err'))
                errors += 1

        if self.combined.get() and self._all_maps:
            self._write_combined_internal()

        parts = []
        if counted:
            parts.append(f"{counted} mapped")
        if errors:
            parts.append(f"{errors} errors")
        if skipped:
            parts.append(f"{skipped} skipped (min-lines)")
        self._q.put(('done', ', '.join(parts)))

    # ##[Worker: Status]

    def _cmd_status(self) -> None:
        folder = self.source_folder.get().strip() or '.'
        if not Path(folder).is_dir():
            messagebox.showerror('Not Found', f'Folder not found:\n{folder}')
            return
        self._start_worker(self._do_status, (folder,))

    def _do_status(self, folder: str) -> None:
        rec = self.recursive.get()
        all_langs = self.all_langs.get()
        ext = None if not all_langs else set(SUPPORTED_EXTENSIONS)
        files = find_source_files([folder], recursive=rec, extra_exts=ext, gitignore_root=Path(folder))
        self._q.put(('clear_results',))
        self._q.put(('progress', f"Checking {len(files)} files\u2026"))

        fresh = stale = missing = 0
        for src in files:
            if self._cancel_flag:
                break
            out = self._get_outpath(src)
            if not out.exists():
                self._q.put(('result', src, None, 'missing'))
                self._q.put(('log', f"  MISSING {src.name}", 'warn'))
                missing += 1
            elif is_stale(src, out):
                self._q.put(('result', src, None, 'stale'))
                self._q.put(('log', f"  STALE   {src.name}", 'warn'))
                stale += 1
            else:
                try:
                    text, lines = read_source(src)
                    fm = analyze_file(src, text, lines)
                except Exception:
                    fm = None
                self._q.put(('result', src, fm, 'fresh'))
                fresh += 1

        self._q.put(('log', f"Summary: {fresh} fresh, {stale} stale, {missing} missing", 'bold'))
        self._q.put(('done', f"{fresh} fresh, {stale} stale, {missing} missing"))

    # ##[Worker: Update]

    def _cmd_update(self) -> None:
        folder = self.source_folder.get().strip() or '.'
        if not Path(folder).is_dir():
            messagebox.showerror('Not Found', f'Folder not found:\n{folder}')
            return
        self._start_worker(self._do_update, (folder,))

    def _do_update(self, folder: str) -> None:
        rec = self.recursive.get()
        all_langs = self.all_langs.get()
        ext = None if not all_langs else set(SUPPORTED_EXTENSIONS)
        files = find_source_files([folder], recursive=rec, extra_exts=ext, gitignore_root=Path(folder))
        major = self.major_only.get()
        force = self.force.get()
        self._q.put(('clear_results',))
        self._q.put(('progress', f"Scanning {len(files)} files\u2026"))

        updated = errors = skipped = fresh_count = 0
        savings_total = 0
        cost_path = Path(self.output_dir.get().strip() or folder) / 'llmeshowyou_cost.json'
        tracker = CostTracker(cost_path)

        for src in files:
            if self._cancel_flag:
                break
            out = self._get_outpath(src)

            if not force and out.exists() and not is_stale(src, out):
                self._q.put(('result', src, None, 'fresh', '', 0))
                fresh_count += 1
                continue

            if not out.exists():
                out.touch()
                out.write_text('', encoding='utf-8')
                out.unlink()

            try:
                text, lines = read_source(src)
                fm = analyze_file(src, text, lines)

                if major and not force:
                    if not is_major_change(src, out, fm):
                        skipped += 1
                        continue

                write_map(fm, out)
                savings = self._calc_savings(src, fm)
                savings_total += savings
                self._q.put(('result', src, fm, 'mapped', '', savings))
                self._q.put(('log', f"  update {src.name} ({fm.lines} l, {fm.total_symbols} sym, lang={fm.language}, saves ~{LLMEShowYouGUI._fmt_savings(savings)} tok)", 'ok'))
                self._all_maps.append(fm)
                updated += 1
            except SyntaxError as e:
                self._q.put(('result', src, None, 'error'))
                self._q.put(('log', f"  syntax error {src.name}: {e}", 'err'))
                errors += 1
            except Exception as e:
                self._q.put(('result', src, None, 'error'))
                self._q.put(('log', f"  error {src.name}: {e}", 'err'))
                errors += 1

        if updated and savings_total:
            tracker.add('update', updated, savings_total)
            self._q.put(('log', f"  cumulative tokens saved: {fmt_tokens(tracker.total_saved)}", 'god'))

        if self.combined.get() and self._all_maps:
            self._write_combined_internal()

        parts = []
        if updated:
            parts.append(f"{updated} updated")
        if fresh_count:
            parts.append(f"{fresh_count} fresh")
        if skipped:
            parts.append(f"{skipped} minor-only skipped")
        if errors:
            parts.append(f"{errors} errors")
        self._q.put(('done', ', '.join(parts)))

    # ##[Worker: Update --check]

    def _cmd_update_check(self) -> None:
        folder = self.source_folder.get().strip() or '.'
        if not Path(folder).is_dir():
            messagebox.showerror('Not Found', f'Folder not found:\n{folder}')
            return
        self._start_worker(self._do_update_check, (folder,))

    def _do_update_check(self, folder: str) -> None:
        rec = self.recursive.get()
        all_langs = self.all_langs.get()
        ext = None if not all_langs else set(SUPPORTED_EXTENSIONS)
        files = find_source_files([folder], recursive=rec, extra_exts=ext, gitignore_root=Path(folder))
        self._q.put(('clear_results',))
        self._q.put(('progress', f"Checking {len(files)} files\u2026"))

        stale_list = []
        fresh_count = 0
        for src in files:
            if self._cancel_flag:
                break
            out = self._get_outpath(src)
            if not out.exists() or is_stale(src, out):
                self._q.put(('result', src, None, 'stale'))
                self._q.put(('log', f"  stale  {src.name}", 'warn'))
                stale_list.append(src.name)
            else:
                fm = None
                try:
                    text, lines = read_source(src)
                    fm = analyze_file(src, text, lines)
                except Exception:
                    pass
                self._q.put(('result', src, fm, 'fresh'))
                fresh_count += 1

        if stale_list:
            msg = f"{len(stale_list)} stale: {', '.join(stale_list[:5])}"
            if len(stale_list) > 5:
                msg += f" \u2026 and {len(stale_list) - 5} more"
            self._q.put(('log', f"OUTDATED: {len(stale_list)} file(s) need updating.", 'err'))
            self._q.put(('done', msg))
        else:
            self._q.put(('log', "All maps fresh.", 'ok'))
            self._q.put(('done', 'all fresh'))

    # ##[Worker: Graph]

    def _cmd_graph(self) -> None:
        folder = self.source_folder.get().strip() or '.'
        if not Path(folder).is_dir():
            messagebox.showerror('Not Found', f'Folder not found:\n{folder}')
            return
        self._start_worker(self._do_graph, (folder,))

    def _do_graph(self, folder: str) -> None:
        all_langs = self.all_langs.get()
        ext = set() if not all_langs else set(SUPPORTED_EXTENSIONS)
        files = find_source_files([folder], recursive=True, extra_exts=ext, gitignore_root=Path(folder))
        self._q.put(('clear_results',))
        self._q.put(('progress', f"Analyzing {len(files)} files\u2026"))

        maps: list[FileMap] = []
        for src in files:
            if self._cancel_flag:
                break
            out = self._get_outpath(src)
            if not out.exists():
                continue
            try:
                text, lines = read_source(src)
                fm = analyze_file(src, text, lines)
                maps.append(fm)
            except Exception:
                continue

        if not maps:
            self._q.put(('log', 'No maps found to build graph.', 'err'))
            self._q.put(('done', '0 maps'))
            return

        graph = build_project_graph(maps)
        gods = rank_god_nodes(graph)

        outdir = Path(self.output_dir.get().strip() or folder)
        html_path = outdir / 'PROJECT_GRAPH.html'
        render_html_index(graph, html_path)
        self._q.put(('log', f"Graph HTML -> {html_path}", 'ok'))

        # Print god nodes
        self._q.put(('log', f"Project Graph: {graph.total_nodes} nodes, {graph.total_edges} edges", 'god'))
        if gods:
            self._q.put(('log', "God Nodes (most imported):", 'bold'))
            for i, (fp, d) in enumerate(gods[:10], 1):
                p = Path(fp)
                self._q.put(('log', f"  {i}. {p.name}  \u2014 imported by {d} file(s)", 'god'))

        # Combined index
        if self.combined.get():
            idx_path = outdir / 'INDEX.map.md'
            from llmeshowyou import _write_combined_index
            _write_combined_index(maps, idx_path)
            self._q.put(('log', f"Index -> {idx_path}", 'ok'))

        self._q.put(('done', f"{graph.total_nodes} nodes, {graph.total_edges} edges"))

    def _show_graph_summary(self, text: str) -> None:
        self._log_msg(text, 'god')

    # ##[Worker: Cost]

    def _cmd_cost(self) -> None:
        folder = self.source_folder.get().strip() or '.'
        self._start_worker(self._do_cost, (folder,))

    def _do_cost(self, folder: str) -> None:
        cost_path = Path(self.output_dir.get().strip() or folder) / 'llmeshowyou_cost.json'
        tracker = load_cost(cost_path)
        self._q.put(('clear_results',))
        if not tracker.records:
            self._q.put(('log', "No cost records found.", 'info'))
            self._q.put(('done', '0 records'))
            return
        self._q.put(('log', f"Cost Tracker: {tracker.path}", 'bold'))
        self._q.put(('log', f"Total tokens saved: {fmt_tokens(tracker.total_saved)}", 'ok'))
        self._q.put(('log', f"Operations: {len(tracker.records)}", 'info'))
        self._q.put(('log', "Recent operations:", 'bold'))
        for r in tracker.records[-10:]:
            self._q.put(('log', f"  {r.date[:19]}  {r.operation:10s}  {r.files_processed:3d} files  saved {fmt_tokens(r.tokens_saved)}", 'info'))
        self._q.put(('done', f"{fmt_tokens(tracker.total_saved)} saved"))

    def _show_cost_summary(self, text: str) -> None:
        self._log_msg(text, 'ok')

    # ##[Worker: Wiki]

    def _cmd_wiki(self) -> None:
        folder = self.source_folder.get().strip() or '.'
        if not Path(folder).is_dir():
            messagebox.showerror('Not Found', f'Folder not found:\n{folder}')
            return
        self._start_worker(self._do_wiki, (folder,))

    def _do_wiki(self, folder: str) -> None:
        from llmeshowyou import render_wiki, _load_graph, build_project_graph, analyze_file, find_source_files
        outdir = Path(self.output_dir.get().strip() or folder) / 'wiki'

        graph = _load_graph(Path(folder))
        if graph is None:
            all_langs = self.all_langs.get()
            ext = set() if not all_langs else set(SUPPORTED_EXTENSIONS)
            files = find_source_files([folder], recursive=True, extra_exts=ext, gitignore_root=Path(folder))
            maps = []
            self._q.put(('progress', f"Reading {len(files)} files\u2026"))
            for src in files:
                if self._cancel_flag:
                    break
                try:
                    text, lines = read_source(src)
                    maps.append(analyze_file(src, text, lines))
                except Exception:
                    continue
            if not maps:
                self._q.put(('log', "No files to build wiki from.", 'err'))
                self._q.put(('done', '0 files'))
                return
            graph = build_project_graph(maps)

        render_wiki(graph, outdir)
        self._q.put(('log', f"Wiki exported to {outdir}", 'ok'))
        self._q.put(('done', f"{graph.total_nodes} pages"))

    # ##[Worker: Hook]

    def _cmd_hook(self, action: str) -> None:
        folder = self.source_folder.get().strip() or '.'
        action_map = {
            'install': install_hook,
            'uninstall': uninstall_hook,
            'status': status_hook,
        }
        fn = action_map.get(action)
        if not fn:
            return

        def _do():
            result = fn(Path(folder))
            if result == 0:
                self._q.put(('hook_result', f"Hook {action} successful.", 'ok'))
            self._q.put(('done', ''))

        self._start_worker(_do)

    # ##[Helpers]

    def _get_outpath(self, src: Path) -> Path:
        outdir = self.output_dir.get().strip()
        if outdir:
            return Path(outdir) / (src.name + '.map.md')
        return map_path_for(src)

    def _read_stored_hash(self, map_path: Path) -> Optional[str]:
        try:
            for line in map_path.read_text(encoding='utf-8').splitlines()[:5]:
                m = re.search(r'sha256:([a-f0-9]+)', line)
                if m:
                    return m.group(1)
        except Exception:
            return None
        return None

    def _get_selected_item(self) -> Optional[dict]:
        sel = self._tree.selection()
        if not sel:
            messagebox.showinfo('No Selection', 'Select a row in the results first.')
            return None
        item = self._tree.item(sel[0])
        name = item['text']
        vals = item['values']
        return {
            'name': name,
            'lines': vals[0],
            'sym': vals[1],
            'status': vals[4] if len(vals) > 4 else '',
            'map_label': vals[5] if len(vals) > 5 else '',
        }

    def _get_selected_items(self) -> list[dict]:
        sel = self._tree.selection()
        items: list[dict] = []
        for iid in sel:
            item = self._tree.item(iid)
            name = item['text']
            vals = item['values']
            items.append({
                'name': name,
                'lines': vals[0] if len(vals) > 0 else '0',
                'sym': vals[1] if len(vals) > 1 else '0',
                'status': vals[4] if len(vals) > 4 else '',
                'map_label': vals[5] if len(vals) > 5 else '',
            })
        return items

    def _get_selected_paths(self) -> tuple[Optional[Path], Optional[Path]]:
        sel = self._get_selected_item()
        if not sel:
            return None, None
        fname = sel['name']
        src = self._find_source_by_name(fname)
        if not src:
            messagebox.showerror('Not Found', f'Could not resolve path for "{fname}"')
            return None, None
        out = self._get_outpath(src)
        return src, out

    def _get_selected_paths_multi(self) -> list[tuple[Path, Path]]:
        items = self._get_selected_items()
        results: list[tuple[Path, Path]] = []
        for sel in items:
            fname = sel['name']
            src = self._find_source_by_name(fname)
            if src:
                out = self._get_outpath(src)
                results.append((src, out))
        return results

    def _find_source_by_name(self, name: str) -> Optional[Path]:
        # Strip language tag if present
        clean = re.sub(r'\s*\[.*?\]\s*$', '', name)
        for src_path in self._result_id_map:
            if src_path.name == clean:
                return src_path
        folder = self.source_folder.get().strip()
        if folder:
            candidate = Path(folder) / clean
            if candidate.is_file():
                return candidate
        return None

    def _find_common_ancestor(self, paths: list[Path]) -> Path:
        if not paths:
            return Path('.')
        common = paths[0]
        for p in paths[1:]:
            try:
                common = Path(os.path.commonpath([str(common), str(p)]))
            except ValueError:
                pass
        if not common.is_dir():
            common = common.parent
        return common

    def _build_multi_prompt(self, items: list[dict]) -> tuple[str, Path, list[str], list[str]]:
        """Build a single-line multi-file prompt safe for cmd.exe embedding.
        Returns (prompt, folder, map_names, py_names)."""
        srcs: list[Optional[Path]] = []
        for sel in items:
            src = self._find_source_by_name(sel['name'])
            if src:
                srcs.append(src)
        if not srcs:
            return '', Path('.'), [], []
        folder = self._find_common_ancestor(srcs)
        map_names: list[str] = []
        py_names: list[str] = []
        for src in srcs:
            try:
                rel = os.path.relpath(str(src), str(folder)).replace('\\', '/')
            except ValueError:
                rel = src.name
            map_names.append(rel + '.map.md')
            py_names.append(rel)

        if len(srcs) == 1:
            prompt = (
                f"Read the file '{map_names[0]}' first to understand the structure of "
                f"'{py_names[0]}', then help me. "
                f"The map shows classes, functions, their signatures, line ranges, "
                f"imports, constants, section markers, and TODOs. "
                f"When reading a .map.md file to understand a source file, and additional "
                f"files are referenced in the code or imports, first search for "
                f"the corresponding .map.md files (e.g., foo.py -> foo.py.map.md) and "
                f"read those maps before falling back to reading the full source. "
                f"Maps provide classes, functions, signatures, line ranges, imports, "
                f"constants, section markers, and TODOs \u2014 use them to identify the "
                f"specific lines to read from source rather than reading entire files."
            )
        else:
            maps_str = ', '.join(map_names)
            files_str = ', '.join(py_names)
            prompt = (
                f"Read the maps '{maps_str}' (for {files_str}) first to understand "
                f"the structure, then help me. "
                f"Each map shows classes, functions, their signatures, line ranges, "
                f"imports, constants, section markers, and TODOs. "
                f"When reading a .map.md file to understand its source file, and additional "
                f"files are referenced in the code or imports, first search for "
                f"the corresponding .map.md files and read those maps before falling back "
                f"to reading the full source. "
                f"Maps provide classes, functions, signatures, line ranges, imports, "
                f"constants, section markers, and TODOs \u2014 use them to identify the "
                f"specific lines to read from source rather than reading entire files."
            )
        return prompt, folder, map_names, py_names

    def _open_selected_map(self) -> None:
        pairs = self._get_selected_paths_multi()
        if not pairs:
            return
        for src, out in pairs:
            target = out if out and out.exists() else src
            if target:
                try:
                    os.startfile(str(target))
                except Exception:
                    subprocess.Popen(['notepad.exe', str(target)])
        n = len(pairs)
        self._log_msg(f"Opened {n} file(s)", 'ok')

    def _reveal_selected(self) -> None:
        pairs = self._get_selected_paths_multi()
        if not pairs:
            return
        # Reveal the first one; if multiple, just open the folder
        if len(pairs) == 1:
            src, out = pairs[0]
            target = out if out and out.exists() else src
            if target:
                subprocess.Popen(['explorer.exe', '/select,', str(target.resolve())])
            return
        folder = self._find_common_ancestor([p[0] for p in pairs])
        if folder.is_dir():
            os.startfile(str(folder))
        self._log_msg(f"Revealed {len(pairs)} file(s)", 'ok')

    def _launch_opencode_selected(self) -> None:
        items = self._get_selected_items()
        if not items:
            return
        self._launch_opencode(items)

    def _launch_claude_code_selected(self) -> None:
        items = self._get_selected_items()
        if not items:
            return
        self._launch_claude_code(items)

    def _launch_opencode(self, items: list[dict]) -> None:
        if not items:
            return
        prompt, folder, map_names, py_names = self._build_multi_prompt(items)
        prompt = self._append_ac_dod(prompt, folder)

        opencode_path = self._find_opencode()
        if not opencode_path:
            self._copy_cmd_to_clipboard(folder, prompt)
            messagebox.showinfo(
                'OpenCode Not Found',
                'opencode was not found on PATH.\n'
                'The command has been copied to your clipboard instead.'
            )
            return

        cmd = f'cd /d "{folder}" && "{opencode_path}" --prompt "{prompt}"'
        full = f'cmd.exe /K {cmd}'

        try:
            subprocess.Popen(
                full,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                shell=False,
            )
            names = ', '.join(py_names[:3])
            if len(py_names) > 3:
                names += f' ... (+{len(py_names)-3})'
            self._log_msg(f"Launched opencode for {names}", 'ok')
        except Exception as e:
            self._log_msg(f"Failed to launch opencode: {e}", 'err')
            self._copy_cmd_to_clipboard(folder, prompt)

    def _launch_claude_code(self, items: list[dict]) -> None:
        if not items:
            return
        prompt, folder, map_names, py_names = self._build_multi_prompt(items)
        prompt = self._append_ac_dod(prompt, folder)

        claude_path = self._find_claude_code()
        if not claude_path:
            self._copy_claude_cmd_to_clipboard(folder, prompt)
            messagebox.showinfo(
                'Claude Code Not Found',
                'claude was not found on PATH.\n'
                'The command has been copied to your clipboard instead.'
            )
            return

        cmd = f'cd /d "{folder}" && "{claude_path}" -p "{prompt}"'
        full = f'cmd.exe /K {cmd}'

        try:
            subprocess.Popen(
                full,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                shell=False,
            )
            names = ', '.join(py_names[:3])
            if len(py_names) > 3:
                names += f' ... (+{len(py_names)-3})'
            self._log_msg(f"Launched claude for {names}", 'ok')
        except Exception as e:
            self._log_msg(f"Failed to launch claude: {e}", 'err')
            self._copy_claude_cmd_to_clipboard(folder, prompt)

    def _copy_opencode_cmd_selected(self) -> None:
        items = self._get_selected_items()
        if not items:
            return
        prompt, folder, _map_names, _py_names = self._build_multi_prompt(items)
        prompt = self._append_ac_dod(prompt, folder)
        self._copy_cmd_to_clipboard(folder, prompt)

    def _copy_claude_code_cmd_selected(self) -> None:
        items = self._get_selected_items()
        if not items:
            return
        prompt, folder, _map_names, _py_names = self._build_multi_prompt(items)
        prompt = self._append_ac_dod(prompt, folder)
        self._copy_claude_cmd_to_clipboard(folder, prompt)

    def _copy_cmd_to_clipboard(self, folder: Path, prompt: str) -> None:
        opencode_path = self._find_opencode() or 'opencode'
        cmd = (
            f'cd /d "{folder}" && {opencode_path} --prompt "{prompt}"'
        )
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(cmd)
            self._status('Command copied to clipboard!')
            self._log_msg('opencode command copied to clipboard', 'ok')
        except Exception as e:
            self._log_msg(f'Clipboard error: {e}', 'err')

    def _copy_claude_cmd_to_clipboard(self, folder: Path, prompt: str) -> None:
        claude_path = self._find_claude_code() or 'claude'
        cmd = (
            f'cd /d "{folder}" && {claude_path} -p "{prompt}"'
        )
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(cmd)
            self._status('Command copied to clipboard!')
            self._log_msg('claude command copied to clipboard', 'ok')
        except Exception as e:
            self._log_msg(f'Clipboard error: {e}', 'err')

    def _append_ac_dod(self, prompt: str, folder: Path) -> str:
        """If enabled, append AC/DoD protocol + feedback instructions."""
        if not self.include_ac_dod.get():
            return prompt

        criteria_line = ''
        feedback = ''
        dpath = self._find_discipline(folder)
        if dpath is not None:
            p_abs = dpath.resolve()
            try:
                import discipline
                fm, body = discipline._load(str(p_abs))
                ticket = fm.get('ticket', '?')
                items = self._extract_ac_items(body)
                if items:
                    crit = '; '.join(items).replace('"', "'")
                    criteria_line = f" Active ticket={ticket}. AC/DoD criteria: {crit}."
                feedback = (
                    f" AC/DoD file: {p_abs}. Update this file as you work: mark "
                    f"criteria with [x] when done, add lines under ## Evidence "
                    f"Ledger (format: - TIMESTAMP [role] what was done), and "
                    f"update the gate status when complete. Read the file first "
                    f"to see its current state."
                )
                self._log_msg(
                    f'AC/DoD appended: ticket={ticket}, {len(items)} criteria', 'ok')
            except Exception as e:
                self._log_msg(f'AC/DoD parse error: {e}', 'err')
        else:
            self._log_msg('AC/DoD: no .discipline.md found (protocol still applied)', 'warn')

        prompt += (
            " --- AC/DoD PROTOCOL --- Before finishing, define and satisfy an "
            "Acceptance Criteria / Definition of Done. State the acceptance "
            "criteria explicitly, then verify each one (build passes, tests pass, "
            "files exist as specified). Do NOT report the task done until every "
            "criterion is met and verified."
            + criteria_line
            + feedback
        )
        return prompt

    def _launch_review(self) -> None:
        """Launch opencode as an independent reviewer — no prior context,
        reads discipline file and source, verifies each AC criterion."""
        dpath = self._discipline_file()
        if not dpath.exists():
            messagebox.showwarning('Review', 'No discipline file. Init one first.')
            return
        import discipline
        try:
            fm, body = discipline._load(str(dpath))
        except Exception as e:
            messagebox.showerror('Review', f'Cannot read discipline file:\n{e}')
            return
        ticket = fm.get('ticket', '?')
        items = self._extract_ac_items(body)
        if not items:
            messagebox.showinfo('Review', 'No AC/DoD criteria to review.')
            return
        scan_root = Path.cwd()
        folder = scan_root
        opencode_path = self._find_opencode()
        if not opencode_path:
            messagebox.showinfo(
                'Review', 'opencode not found on PATH.\n'
                'Install it or launch a session manually with:\n'
                f'  cd /d "{folder}" && opencode --prompt "..."')
            return
        crit = '; '.join(s for s in items)
        skip_dirs = {'node_modules', '__pycache__', '.git', '.opencode',
                     '.claude', 'build', 'dist', '.aider', 'model_temp',
                     'map_cache', 'venv', '.venv'}
        map_sources = []
        for m in sorted(scan_root.rglob('*.map.md')):
            if any(p.name in skip_dirs or p.name.startswith('.')
                   for p in m.relative_to(scan_root).parents):
                continue
            try:
                rel = str(m.relative_to(scan_root)).replace('\\', '/')
                src = rel.removesuffix('.map.md')
                if m.parent != scan_root or not (scan_root / src).exists():
                    continue
                map_sources.append((rel, src))
            except Exception:
                continue
        if map_sources:
            if len(map_sources) == 1:
                mn, pn = map_sources[0]
                map_ref = (
                    f"Read the file '{mn}' first to understand the structure of "
                    f"'{pn}', then verify it. "
                    f"The map shows classes, functions, their signatures, line ranges, "
                    f"imports, constants, section markers, and TODOs. "
                )
            else:
                maps_str = ', '.join(mn for mn, _ in map_sources)
                files_str = ', '.join(pn for _, pn in map_sources)
                map_ref = (
                    f"Read the maps '{maps_str}' (for {files_str}) first to understand "
                    f"the structure, then verify each. "
                    f"Each map shows classes, functions, their signatures, line ranges, "
                    f"imports, constants, section markers, and TODOs. "
                )
        else:
            map_ref = ''
        prompt = (
            f"{map_ref}"
            f"When reading a .map.md file to understand a source file, and additional "
            f"files are referenced in the code or imports, first search for "
            f"the corresponding .map.md files and read those maps before falling back "
            f"to reading the full source. "
            f"Maps provide classes, functions, signatures, line ranges, imports, "
            f"constants, section markers, and TODOs \u2014 use them to identify the "
            f"specific lines to read from source rather than reading entire files. --- "
            f"Reviewer — ticket={ticket} discipline={dpath} criteria={crit}. "
            f"For each criterion read relevant source and report PASS or FAIL. "
            f"After verifying ALL: mark each [x], add findings under ## Evidence "
            f"Ledger, gate=pass if all pass gate=fail if any fail, "
            f"phase=review. Do NOT trust implementer — verify from source."
        )
        cmd = (f'cd /d "{folder}" && "{opencode_path}" --prompt "{prompt}"')
        full = f'cmd.exe /K {cmd}'
        try:
            subprocess.Popen(full, creationflags=subprocess.CREATE_NEW_CONSOLE,
                             shell=False)
            self._log_msg(f'Review launched for ticket={ticket}', 'ok')
        except Exception as e:
            self._log_msg(f'Failed to launch review: {e}', 'err')

    def _discipline_file(self) -> Path:
        """Return the absolute discipline file path from config, or cwd fallback."""
        p = self.discipline_path.get().strip()
        if p:
            return Path(p).resolve()
        return Path.cwd() / '.discipline.md'

    @staticmethod
    def _extract_ac_items(body: str) -> list[str]:
        """Return only the checklist items under the '## AC/DoD' section."""
        items: list[str] = []
        in_ac = False
        for ln in body.splitlines():
            s = ln.strip()
            if s.startswith('## '):
                in_ac = s[3:].strip().lower().startswith('ac/dod')
                continue
            if in_ac and (s.startswith('- [ ]') or s.startswith('- [x]')):
                items.append(s)
        return items

    def _find_discipline(self, start: Path) -> Optional[Path]:
        """Return the configured discipline file if it exists, otherwise scan
        the selected-files folder tree and cwd."""
        dpath = self._discipline_file()
        if dpath.exists():
            return dpath
        roots = [start, Path.cwd()]
        seen: set[Path] = set()
        for root in roots:
            try:
                cur = root.resolve()
            except Exception:
                continue
            for _ in range(8):
                if cur in seen:
                    break
                seen.add(cur)
                dpath = cur / '.discipline.md'
                if dpath.exists():
                    return dpath
                parent = cur.parent
                if parent == cur:
                    break
                cur = parent
        return None

    def _find_opencode(self) -> Optional[str]:
        exe = shutil.which('opencode')
        if exe:
            return exe
        npm = Path(os.environ.get('APPDATA', '')) / 'npm' / 'opencode.cmd'
        if npm.exists():
            return str(npm)
        npm2 = Path(os.environ.get('APPDATA', '')) / 'npm' / 'opencode'
        if npm2.exists():
            return str(npm2)
        return None

    def _find_claude_code(self) -> Optional[str]:
        exe = shutil.which('claude')
        if exe:
            return exe
        npm = Path(os.environ.get('APPDATA', '')) / 'npm' / 'claude'
        if npm.exists():
            return str(npm)
        npm2 = Path(os.environ.get('APPDATA', '')) / 'npm' / 'claude.cmd'
        if npm2.exists():
            return str(npm2)
        return None

    def _open_index_map(self) -> None:
        folder = self.source_folder.get().strip() or '.'
        idx = Path(folder) / 'INDEX.map.md'
        if self.output_dir.get().strip():
            idx = Path(self.output_dir.get().strip()) / 'INDEX.map.md'
        if idx.exists():
            try:
                os.startfile(str(idx))
            except Exception:
                subprocess.Popen(['notepad.exe', str(idx)])
        else:
            messagebox.showinfo('Not Found', 'INDEX.map.md not found. Run Map Folder with --combined first.')

    def _write_combined_internal(self) -> None:
        folder = Path(self.source_folder.get().strip() or '.')
        idx_path = Path(self.output_dir.get().strip()) if self.output_dir.get().strip() else folder
        idx_file = idx_path / 'INDEX.map.md'
        self._q.put(('combined', str(idx_file)))

    def _write_combined(self, path: str) -> None:
        maps = self._all_maps
        if not maps:
            return
        idx_path = Path(path)
        dest_dir = idx_path.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# Index: Source File Maps",
            f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "",
        ]
        for fm in maps:
            name = Path(fm.source_path).name
            map_name = name + '.map.md'
            lines.append(f"- [{name}]({map_name}) \u2014 {fm.lines} lines, {fm.total_symbols} symbols ({fm.language})")
        idx_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        self._log_msg(f"Combined index -> {idx_path.name}", 'ok')

    # ##[Help System]

    def _show_help(self) -> None:
        win = tk.Toplevel(self.root)
        win.title('llmeshowyou \u2014 How to Use')
        win.geometry('820x680')
        win.minsize(500, 400)
        win.transient(self.root)

        text = ScrolledText(win, wrap='word', state='normal',
                             font=('Segoe UI', 10), bg='#ffffff', fg='#1e1e1e',
                             padx=16, pady=12, borderwidth=0, highlightthickness=0)
        text.pack(fill='both', expand=True)

        text.tag_config('h1', font=('Segoe UI', 16, 'bold'), spacing3=10)
        text.tag_config('h2', font=('Segoe UI', 13, 'bold'), spacing3=8, foreground='#1565c0')
        text.tag_config('h3', font=('Segoe UI', 11, 'bold'), spacing3=6)
        text.tag_config('body', font=('Segoe UI', 10), spacing1=1, spacing2=3)
        text.tag_config('code', font=('Consolas', 9), foreground='#1e1e1e', background='#f0f0f0')
        text.tag_config('mono', font=('Consolas', 9))
        text.tag_config('key', font=('Segoe UI', 9, 'bold'), background='#e0e0e0', foreground='#333')
        text.tag_config('emph', foreground='#1565c0')
        text.tag_config('ok', foreground='#2e7d32')
        text.tag_config('link', foreground='#1565c0', underline=True)

        content = """\
Overview
llmeshowyou maps source files (Python, JS, TS, Go, Rust, Java, C/C++, and many more) \
to compact markdown so an LLM can understand your code structure without consuming \
the full source text. The markdown map includes classes, methods, function signatures, \
imports, constants, section markers, and TODOs — all with accurate line ranges.

New in v2.0: Cross-File Import Graph
The Graph feature builds a cross-file import graph from your .map.md files, ranks \
"god nodes" (the most-imported files), and generates an interactive PROJECT_GRAPH.html \
with clickable node cards showing incoming/outgoing edges.

New in v2.0: Multi-Language Support
Check "All Languages" to map .js, .ts, .go, .rs, .java, .c, .cpp, .rb, .cs, .kt, \
.swift, .php, .lua, .zig, .dart, .jl, .sh, and more. Tree-sitter is used when \
available; a regex fallback handles basic structure extraction otherwise.

New in v2.0: Query, Path, Explain
Right-click a result row and choose "Explain in Graph" (shows full node info with \
dependencies and dependents) or "Find Shortest Path" (BFS through the import graph).

New in v2.0: Wiki Export
Export Wiki generates an Obsidian-compatible markdown wiki with one page per file, \
wikilinks for dependencies, and an index page.

New in v2.0: Cost Tracker
The Cost Tracker persists token savings across sessions. Click Cost to see \
cumulative savings and per-operation breakdowns.

New in v2.0: Git Hook
Install the git post-commit hook to auto-regenerate stale maps every time \
you commit. Available from Tools > Install Git Hook.

New in v2.0: Export Formats
The command-line tool supports --graphml (Gephi/yEd) and --neo4j (Cypher) exports.

New in v2.0: Watch Mode
Check Watch Mode (Options bar) to automatically regenerate maps as files change \
(uses polling — works on any filesystem).

How Staleness Detection Works
When a .map.md file is generated, it embeds a SHA-256 hash of the source \
in its metadata header. Every time Status or Update runs, the tool:
  1) Reads the stored hash from the .map.md file
  2) Recomputes the hash of the current source file
  3) If they differ → the map is stale (the source was modified)
  4) If no .map.md exists → status shows "missing"

ANY edit to the source — even a single character — triggers stale. The \
checkbox Major Only adds a second pass: compares the symbol set \
(class/function names) from the old map versus the new AST, and checks if \
total line count changed by more than 20%. If only comments or whitespace \
changed, it's considered minor and skipped during Update.

How to Use — Step by Step
1. Select a file or folder
   File > Open Source File (Ctrl+O) or click Browse next to the File field. \
   Use Open Folder (Ctrl+Shift+O) to batch-process a whole directory. \
   Recent files are remembered across sessions.

2. Configure options
   Recursive — include subdirectories when processing a folder.\n
   Combined Index — generate INDEX.map.md linking all maps in one place.\n
   Force Remap — re-map even if the source hash hasn't changed.\n
   Major Only — skip cosmetic-only updates.\n
   All Languages — include non-Python files (.js, .ts, .go, etc.).\n
   Watch Mode — auto-regenerate maps as files change.\n
   Min lines — skip files smaller than this threshold.

3. Map the file(s)
   Map File (F5) — create or refresh the .map.md for a single file.\n
   Map Folder (F6) — map every source file in the selected folder.\n
   The results appear in the table with status, line count, and symbols.

4. Explore results
   Double-click a row → launch OpenCode (TUI) with the map as the prompt.\n
   Right-click a row → context menu (Open .map.md, OpenCode, Copy Cmd, \
   Reveal in Explorer, Explain in Graph, Find Shortest Path).\n
   Select a row and use the buttons below the results.

5. Build the project graph
   Click Graph to build a cross-file import graph. Opens PROJECT_GRAPH.html \
   in your browser with color-coded node cards and god-node rankings.

6. Keep maps updated
   Status (F7) — check freshness without modifying anything.\n
   Update (F8) — remap only files whose SHA-256 hash has changed.\n
   Update --check (F9) — CI-style check: listing of stale files.

Feature Reference
Map File (F5) — Analyze a single source file and write its .map.md sibling. \
Shows lines, symbols, and a link to the generated map.

Map Folder (F6) — Batch-map all source files in a folder. Respects Recursive, \
Min Lines, and All Languages options. With Combined Index checked, also writes \
INDEX.map.md.

Status (F7) — Walk a folder and classify each file as fresh, stale, or \
missing based on SHA-256 comparison. Does not write or modify anything.

Update (F8) — Walk a folder and remap only stale files. With Major Only \
checked, skips cosmetic edits. With Force Remap, ignores hash and remaps \
everything.

Update --check (F9) — Read-only check that lists stale files. Exits with \
a summary of how many need updating.

Graph — Build a cross-file import dependency graph from existing maps. \
Generates PROJECT_GRAPH.html with god-node rankings.

Cost — Show cumulative token savings across sessions.

Wiki — Export an Obsidian-compatible wiki (one page per file with \
wikilinks for dependencies).

Graph Menu — Right-click a result > Explain in Graph (show node details \
with deps) or Find Shortest Path (BFS path between two files).

Git Hook — Install post-commit hook to auto-regenerate stale maps.

Watch Mode — Auto-regenerate maps as files change (polling-based).

OpenCode Integration
When you click Open in OpenCode, the tool runs the following in a new \
console window:

cmd.exe /K cd /d "<source_folder>" && opencode --prompt "Read the \
map first..."

This opens the interactive opencode TUI (the "command prompt mode", not \
Windows GUI) with the working directory set to the source file's folder. \
The prompt tells the LLM to read the .map.md first for structure, then \
reference the source file by line range. This dramatically reduces \
token usage and makes large files navigable.

Keyboard Shortcuts
Ctrl+O          Open Source File
Ctrl+Shift+O    Open Folder
F1              Help
F5              Map File
F6              Map Folder
F7              Status
F8              Update
F9              Update --check"""

        text.insert('1.0', content, 'body')
        self._style_help_text(text)
        text.config(state='disabled')
        ttk.Button(win, text='Close', command=win.destroy).pack(pady=(4, 8))

    def _style_help_text(self, text: ScrolledText) -> None:
        lines = text.get('1.0', 'end').splitlines(keepends=True)

        for i, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            stripped = line.strip()
            if stripped in (
                'Overview',
                'New in v2.0: Cross-File Import Graph',
                'New in v2.0: Multi-Language Support',
                'New in v2.0: Query, Path, Explain',
                'New in v2.0: Wiki Export',
                'New in v2.0: Cost Tracker',
                'New in v2.0: Git Hook',
                'New in v2.0: Export Formats',
                'New in v2.0: Watch Mode',
                'How Staleness Detection Works',
                'How to Use \u2014 Step by Step',
                'Feature Reference',
                'OpenCode Integration',
                'Keyboard Shortcuts',
            ):
                text.tag_add('h1', f'{i}.0', f'{i}.0 lineend')
            elif stripped in (
                '1. Select a file or folder',
                '2. Configure options',
                '3. Map the file(s)',
                '4. Explore results',
                '5. Build the project graph',
                '6. Keep maps updated',
            ):
                text.tag_add('h3', f'{i}.0', f'{i}.0 lineend')
            elif stripped.startswith('Map File') or stripped.startswith('Map Folder') or \
                 stripped.startswith('Status') or stripped.startswith('Update') or \
                 stripped.startswith('Update --check') or stripped.startswith('Graph') or \
                 stripped.startswith('Cost') or stripped.startswith('Wiki') or \
                 stripped.startswith('Graph Menu') or stripped.startswith('Git Hook') or \
                 stripped.startswith('Watch Mode') or stripped.startswith('Open .map.md') or \
                 stripped.startswith('Open in OpenCode') or stripped.startswith('Copy opencode Cmd'):
                text.tag_add('h3', f'{i}.0', f'{i}.0 lineend')

            if 'Ctrl+' in stripped or stripped.startswith('F1') or \
               stripped.startswith('F5') or stripped.startswith('F6') or \
               stripped.startswith('F7') or stripped.startswith('F8') or \
               stripped.startswith('F9'):
                for match in __import__('re').finditer(r'(Ctrl\+\w|F[0-9]+)', stripped):
                    start = f'{i}.{match.start()}'
                    end = f'{i}.{match.end()}'
                    text.tag_add('key', start, end)

    # ##[About]

    def _show_about(self) -> None:
        messagebox.showinfo(
            'About llmeshowyou',
            'llmeshowyou \u2014 Multi-Language File Mapper for LLM Context Efficiency\n\n'
            'Generates compact markdown maps of source files so an LLM can\n'
            'understand the structure without consuming the full source.\n\n'
            'Uses AST parsing (Python) / tree-sitter (multi-language) for\n'
            'accurate class/method/function detection with line ranges,\n'
            'signatures, decorators, and docstrings.\n\n'
            'v2.0 adds: import graph, god nodes, wiki export, cost tracker,\n'
            'multi-language support, watch mode, git hooks, query/path/explain,\n'
            'and MCP server.\n\n'
            'Token Savings (typical 2300-line Python file):\n'
            '  Full source:    ~45,000 tokens\n'
            '  Map only:        ~3,300 tokens\n'
            '  Savings per read: ~42,000 tokens (~92% reduction)\n\n'
            'Built for opencode\n\n'
            'Made by Asif Serajian',
        )

    # ##[Discipline Scratchpad]

    def _discipline_path_resolved(self) -> str:
        return str(self._discipline_file().resolve())

    def open_discipline_panel(self):
        import tkinter as tk
        from tkinter import ttk, simpledialog, messagebox, filedialog as fd
        import discipline

        win = tk.Toplevel(self.root); win.title("Discipline Scratchpad"); win.geometry("760x560")
        bar = ttk.Frame(win); bar.pack(fill="x", padx=6, pady=6)
        txt = tk.Text(win, wrap="word", font=("Consolas", 10),
                      bg="#1e1e1e", fg="#dcdcdc", insertbackground="#dcdcdc")
        txt.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        status = ttk.Label(win, text=""); status.pack(fill="x", padx=6, pady=(0, 6))
        path_label = ttk.Label(win, text="", foreground="#888")
        path_label.pack(fill="x", padx=6, pady=(0, 4))

        def dfile() -> str:
            return self._discipline_path_resolved()

        def refresh():
            p = dfile()
            try:
                with open(p, encoding="utf-8") as f:
                    txt.delete("1.0", "end"); txt.insert("1.0", f.read())
                s = discipline.status(p)
                warn = "  [CEILING TRIPPED -> @tdm]" if s.get("ceiling_tripped") else ""
                status.config(text=f"{s.get('ticket','-')} | phase={s.get('phase')} "
                                   f"gate={s.get('gate')} attempts={s.get('attempts')} "
                                   f"AC {s.get('ac_done')}/{s.get('ac_total')}{warn}")
            except FileNotFoundError:
                txt.delete("1.0", "end"); txt.insert("1.0", "(no .discipline.md — click Init)")
                status.config(text="no active task")
            path_label.config(text=f"File: {p}")

        def set_file():
            p = fd.askopenfilename(title="Select .discipline.md",
                                   filetypes=[("discipline", "*.md"), ("All", "*.*")],
                                   parent=win)
            if p:
                self.discipline_path.set(p)
                refresh()

        def do_init():
            p = dfile()
            t = simpledialog.askstring("Init", "Ticket ID (e.g. NQ-123):", parent=win)
            if not t:
                return
            ac = simpledialog.askstring("Init", "AC/DoD, one per line:", parent=win)
            crit = [c.strip() for c in (ac or "").splitlines() if c.strip()]
            try:
                discipline.init_discipline(t, crit, path=p, overwrite=True)
                self.discipline_path.set(p)
                refresh()
            except Exception as e:
                messagebox.showerror("Init failed", str(e), parent=win)

        def do_save():
            with open(dfile(), "w", encoding="utf-8") as f:
                f.write(txt.get("1.0", "end-1c"))
            refresh()

        def gate(state):
            try:
                discipline.set_gate(gate=state, note=f"manual gate={state} via GUI",
                                    bump_attempt=(state == "fail"), path=dfile()); refresh()
            except Exception as e:
                messagebox.showerror("Gate", str(e), parent=win)

        ttk.Button(bar, text="Set File...", command=set_file).pack(side="left", padx=2)
        ttk.Button(bar, text="Init", command=do_init).pack(side="left", padx=2)
        ttk.Button(bar, text="Refresh", command=refresh).pack(side="left", padx=2)
        ttk.Button(bar, text="Save", command=do_save).pack(side="left", padx=2)
        ttk.Separator(bar, orient='vertical').pack(side="left", fill='y', padx=6)
        ttk.Button(bar, text="Review \U0001f50d", command=self._launch_review).pack(side="left", padx=2)
        ttk.Button(bar, text="Pass \u2713", command=lambda: gate("pass")).pack(side="right", padx=2)
        ttk.Button(bar, text="Fail \u2717", command=lambda: gate("fail")).pack(side="right", padx=2)
        refresh()


if __name__ == '__main__':
    LLMEShowYouGUI()
