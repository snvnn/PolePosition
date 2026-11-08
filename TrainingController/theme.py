"""Shared cockpit palette; presentation only, no device or driving side effects."""
from tkinter import ttk
BG='#101722'
PANEL='#182332'
TEXT='#e8eef6'
MUTED='#a7b5c7'
ACCENT='#52d6c3'

def apply(root):
    root.configure(background=BG)
    root.option_add('*Font', ('Malgun Gothic',10))
    root.option_add('*TCombobox*Listbox.background', PANEL)
    root.option_add('*TCombobox*Listbox.foreground', TEXT)
    root.option_add('*TCombobox*Listbox.selectBackground', '#285267')
    style=ttk.Style(root);style.theme_use('clam')
    style.configure('.',background=PANEL,foreground=TEXT,font=('Malgun Gothic',10))
    style.configure('TFrame',background=PANEL)
    style.configure('Shell.TFrame',background=BG)
    style.configure('TLabel',background=PANEL,foreground=TEXT,padding=(0,3))
    style.configure('Title.TLabel',background=BG,foreground=TEXT,font=('Malgun Gothic',21,'bold'))
    style.configure('Subtitle.TLabel',background=BG,foreground=MUTED)
    style.configure('Section.TLabel',foreground=ACCENT,font=('Malgun Gothic',11,'bold'))
    style.configure('Muted.TLabel',foreground=MUTED)
    style.configure('Status.TLabel',foreground=ACCENT,font=('Malgun Gothic',13,'bold'))
    style.configure('TEntry',fieldbackground='#101b29',foreground=TEXT,insertcolor=TEXT,bordercolor='#35465c',padding=7)
    style.configure('TCombobox',fieldbackground='#101b29',foreground=TEXT,arrowcolor=ACCENT,padding=7)
    style.map('TCombobox',fieldbackground=[('readonly','#101b29'),('disabled',PANEL)],foreground=[('disabled','#738297')])
    style.configure('TButton',background='#29394e',foreground=TEXT,borderwidth=0,padding=(14,9))
    style.map('TButton',background=[('active','#3c536c')],foreground=[('disabled','#7a889b')])
    style.configure('Primary.TButton',background=ACCENT,foreground='#10242b',font=('Malgun Gothic',10,'bold'))
    style.map('Primary.TButton',background=[('disabled','#29454b'),('active','#87efdf')],foreground=[('disabled','#9bafb5')])
    style.configure('Danger.TButton',background='#953c4a',foreground='#ffffff')
    style.map('Danger.TButton',background=[('disabled','#3b2d3b'),('active','#bd5263')],foreground=[('disabled','#9c8592')])
    return style
