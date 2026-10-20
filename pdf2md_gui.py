#!/usr/bin/env python3
"""Desktop interface for converting PDF files to Markdown with pdf2md."""
from __future__ import annotations

import queue
import threading
from argparse import Namespace
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from pdf2md import Converter


class Pdf2MdApp(ttk.Frame):
    """A small, dependency-free GUI around the existing Converter class."""

    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master, padding=18)
        self.master = master
        self.selected: list[Path] = []
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.output_dir = tk.StringVar()
        self.ocr = tk.StringVar(value="auto")
        self.tables = tk.StringVar(value="auto")
        self.images = tk.BooleanVar(value=True)
        self.dpi = tk.IntVar(value=150)
        self.status = tk.StringVar(value="Choose one or more PDF files to begin.")

        master.title("PDF to Markdown")
        master.minsize(680, 460)
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._build()
        master.after(100, self._read_events)

    def _build(self) -> None:
        files = ttk.LabelFrame(self, text="PDF files", padding=10)
        files.grid(row=0, column=0, sticky="nsew")
        files.columnconfigure(0, weight=1)
        files.rowconfigure(0, weight=1)
        self.file_list = tk.Listbox(files, height=9, selectmode=tk.EXTENDED)
        self.file_list.grid(row=0, column=0, rowspan=2, sticky="nsew")
        scroll = ttk.Scrollbar(files, orient="vertical", command=self.file_list.yview)
        scroll.grid(row=0, column=1, rowspan=2, sticky="ns")
        self.file_list.configure(yscrollcommand=scroll.set)
        self.add_button = ttk.Button(files, text="Add PDFs…", command=self._add_files)
        self.add_button.grid(row=0, column=2, padx=(10, 0), sticky="new")
        self.remove_button = ttk.Button(files, text="Remove selected", command=self._remove_files)
        self.remove_button.grid(row=1, column=2, padx=(10, 0), sticky="sew")

        destination = ttk.LabelFrame(self, text="Save Markdown in", padding=10)
        destination.grid(row=2, column=0, pady=(14, 0), sticky="ew")
        destination.columnconfigure(0, weight=1)
        ttk.Entry(destination, textvariable=self.output_dir).grid(row=0, column=0, sticky="ew")
        self.browse_button = ttk.Button(destination, text="Choose folder…", command=self._choose_folder)
        self.browse_button.grid(row=0, column=1, padx=(10, 0))

        options = ttk.LabelFrame(self, text="Options", padding=10)
        options.grid(row=3, column=0, pady=(14, 0), sticky="ew")
        ttk.Label(options, text="OCR:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(options, textvariable=self.ocr, values=("auto", "off", "force"), state="readonly", width=9).grid(row=0, column=1, padx=(6, 18))
        ttk.Label(options, text="Tables:").grid(row=0, column=2, sticky="w")
        ttk.Combobox(options, textvariable=self.tables, values=("auto", "lines", "off"), state="readonly", width=9).grid(row=0, column=3, padx=(6, 18))
        ttk.Checkbutton(options, text="Extract figures", variable=self.images).grid(row=0, column=4, sticky="w")
        ttk.Label(options, text="Figure DPI:").grid(row=1, column=0, pady=(8, 0), sticky="w")
        ttk.Spinbox(options, from_=72, to=600, increment=25, textvariable=self.dpi, width=8).grid(row=1, column=1, padx=(6, 18), pady=(8, 0), sticky="w")

        footer = ttk.Frame(self)
        footer.grid(row=4, column=0, pady=(16, 0), sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status, wraplength=500).grid(row=0, column=0, sticky="w")
        self.convert_button = ttk.Button(footer, text="Convert to Markdown", command=self._start_conversion)
        self.convert_button.grid(row=0, column=1, padx=(12, 0))

    def _add_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Choose PDF files", filetypes=(("PDF files", "*.pdf"), ("All files", "*.*")))
        existing = {str(p).lower() for p in self.selected}
        for name in paths:
            path = Path(name)
            if path.suffix.lower() == ".pdf" and str(path).lower() not in existing:
                self.selected.append(path)
                existing.add(str(path).lower())
                self.file_list.insert(tk.END, str(path))
        self.status.set(f"{len(self.selected)} PDF file(s) selected.")

    def _remove_files(self) -> None:
        indexes = list(self.file_list.curselection())
        for index in reversed(indexes):
            del self.selected[index]
            self.file_list.delete(index)
        self.status.set(f"{len(self.selected)} PDF file(s) selected.")

    def _choose_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Choose destination folder", mustexist=False)
        if chosen:
            self.output_dir.set(chosen)

    def _start_conversion(self) -> None:
        if not self.selected:
            messagebox.showwarning("No PDFs selected", "Add at least one PDF file first.")
            return
        if not self.output_dir.get().strip():
            messagebox.showwarning("No destination", "Choose where to save the Markdown files.")
            return
        output = Path(self.output_dir.get()).expanduser()
        try:
            output.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Destination unavailable", f"Cannot create that folder:\n{exc}")
            return

        stems = [p.stem.lower() for p in self.selected]
        if len(stems) != len(set(stems)):
            if not messagebox.askyesno("Matching filenames", "Some selected PDFs have the same name. Their Markdown files may overwrite each other. Continue?"):
                return
        args = Namespace(dpi=self.dpi.get(), image_format="png", images=self.images.get(),
                         tables=self.tables.get(), ocr=self.ocr.get(), ocr_lang="eng",
                         page_markers=False, keep_headers=False, merge_gap=8, min_fig=36)
        self._set_busy(True)
        self.status.set("Starting conversion…")
        threading.Thread(target=self._convert, args=(list(self.selected), output, args), daemon=True).start()

    def _convert(self, files: list[Path], output: Path, args: Namespace) -> None:
        completed, failures = [], []
        for number, pdf in enumerate(files, 1):
            self.events.put(("status", f"Converting {number} of {len(files)}: {pdf.name}"))
            try:
                completed.append(Converter(args).convert(pdf, output))
            except Exception as exc:  # individual PDFs should not stop the batch
                failures.append(f"{pdf.name}: {exc}")
        if failures:
            detail = "\n".join(failures)
            self.events.put(("error", f"Converted {len(completed)} file(s).\n\nCould not convert:\n{detail}"))
        else:
            self.events.put(("done", f"Converted {len(completed)} file(s) to:\n{output}"))

    def _read_events(self) -> None:
        try:
            while True:
                kind, message = self.events.get_nowait()
                if kind == "status":
                    self.status.set(message)
                elif kind == "done":
                    self._set_busy(False)
                    self.status.set("Conversion complete.")
                    messagebox.showinfo("PDF to Markdown", message)
                else:
                    self._set_busy(False)
                    self.status.set("Conversion finished with errors.")
                    messagebox.showerror("PDF to Markdown", message)
        except queue.Empty:
            pass
        self.master.after(100, self._read_events)

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for widget in (self.add_button, self.remove_button, self.browse_button, self.convert_button):
            widget.configure(state=state)


def main() -> None:
    root = tk.Tk()
    Pdf2MdApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
