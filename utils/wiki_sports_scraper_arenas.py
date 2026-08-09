import os
import io
import re
import threading
import datetime
import tkinter as tk
from tkinter import filedialog, ttk, scrolledtext
import requests
import pandas as pd

class SportsScraperApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Sports Data Lab - ARENAS & STADIUMS ONLY")
        self.root.geometry("900x700")
        
        self.save_directory = tk.StringVar()
        
        # --- GUI Setup ---
        dir_frame = tk.Frame(root)
        dir_frame.pack(pady=10, fill=tk.X, padx=10)
        
        tk.Label(dir_frame, text="Save Location:").pack(side=tk.LEFT)
        tk.Entry(dir_frame, textvariable=self.save_directory, width=70, state='readonly').pack(side=tk.LEFT, padx=10)
        tk.Button(dir_frame, text="Browse...", command=self.choose_directory).pack(side=tk.LEFT)
        
        control_frame = tk.Frame(root)
        control_frame.pack(pady=5, fill=tk.X, padx=10)
        self.start_btn = tk.Button(control_frame, text="Start Arena Scrape", command=self.start_scraping, bg="green", fg="white", font=("Arial", 10, "bold"))
        self.start_btn.pack(side=tk.LEFT)
        
        self.progress = ttk.Progressbar(root, orient=tk.HORIZONTAL, length=880, mode='determinate')
        self.progress.pack(pady=10, padx=10)
        
        self.log_area = scrolledtext.ScrolledText(root, wrap=tk.WORD, width=105, height=32, state='disabled', bg="#1e1e1e", fg="#00ff00", font=("Consolas", 9))
        self.log_area.pack(pady=10, padx=10)
        
        self.log_msg("System ready. Select your 'allSports' folder and click Start.")

    def choose_directory(self):
        folder_selected = filedialog.askdirectory()
        if folder_selected:
            self.save_directory.set(folder_selected)
            self.log_msg(f"Target directory set to: {folder_selected}")

    def save_error_log(self, message):
        target_dir = self.save_directory.get()
        if not target_dir: return  
            
        log_path = os.path.join(target_dir, "error_log.txt")
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] ERROR: {message}\n")
        except Exception as e:
            print(f"CRITICAL: Failed to write to error log file: {e}")

    def log_msg(self, message, msg_type="INFO"):
        self.log_area.config(state='normal')
        if msg_type == "ERROR":
            self.log_area.insert(tk.END, f"[{msg_type}] {message}\n", "error")
            self.log_area.tag_config("error", foreground="#ff4444")
            self.save_error_log(message)
        elif msg_type == "PASS":
            self.log_area.insert(tk.END, f"[{msg_type}] {message}\n", "pass")
            self.log_area.tag_config("pass", foreground="#00ffff")
        elif msg_type == "WARN":
            self.log_area.insert(tk.END, f"[{msg_type}] {message}\n", "warn")
            self.log_area.tag_config("warn", foreground="#ffff00")
        else:
            self.log_area.insert(tk.END, f"[{msg_type}] {message}\n")
            
        self.log_area.see(tk.END)
        self.log_area.config(state='disabled')
        self.root.update_idletasks()

    def start_scraping(self):
        target_dir = self.save_directory.get()
        if not target_dir:
            self.log_msg("Please select a save location first.", "ERROR")
            return
            
        self.start_btn.config(state=tk.DISABLED)
        self.progress['value'] = 0
        self.log_msg("Initializing Stadiums & Arenas Scrape Sequence...")
        
        thread = threading.Thread(target=self.run_scraper, args=(target_dir,))
        thread.daemon = True
        thread.start()

    # ==========================================
    # Robust Scraping Engine
    # ==========================================
    
    def fetch_html_with_headers(self, url):
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'
        }
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        return response.text

    def scrape_wiki_table(self, url, match_str, save_path, log_name):
        try:
            html = self.fetch_html_with_headers(url)
            tables = pd.read_html(io.StringIO(html), match=match_str)
            
            if tables:
                df = tables[0]
                
                # 1. Flatten Columns safely (FIX FOR INTEGER BUG)
                # Safely convert to strings before joining to prevent 'int is not iterable' crash
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = ['_'.join(map(str, col)).strip() for col in df.columns.values]
                else:
                    df.columns = [str(col).strip() for col in df.columns.values]
                
                # 2. Clean up Wikipedia citation brackets [a], [12]
                df = df.replace(r'\[.*?\]', '', regex=True)
                
                # 3. Remove special Wikipedia symbols (*, †, ‡, ^) from text columns
                for col in df.select_dtypes(include=['object']):
                    df[col] = df[col].astype(str).str.replace(r'[*†‡^]', '', regex=True).str.strip()
                
                # 4. Drop repeating header rows
                first_col = df.columns[0]
                df = df[df[first_col] != first_col]
                
                # 5. Drop messy visual columns
                cols_to_drop = [c for c in df.columns if 'Coordinates' in c or 'Image' in c or 'Ref' in c or 'Map' in c]
                if cols_to_drop:
                    df = df.drop(columns=cols_to_drop)
                    
                df.to_csv(save_path, index=False, encoding='utf-8-sig')
                self.log_msg(f"Saved {len(df)} rows for {log_name}", "PASS")
                return True
            else:
                self.log_msg(f"Table matching '{match_str}' not found on {log_name}", "ERROR")
                return False
        except Exception as e:
            self.log_msg(f"Failed on {log_name} | {str(e)}", "ERROR")
            return False

    def clean_name(self, name):
        """Removes markers and citations from Wikipedia names to generate URLs."""
        name = re.sub(r'\[.*?\]', '', str(name))
        return name.replace('*', '').strip()

    # ==========================================
    # Execution Sequence
    # ==========================================

    def run_scraper(self, base_dir):
        # Build Folder Architecture
        for sport in ['nba', 'mlb', 'nfl']:
            os.makedirs(os.path.join(base_dir, sport, 'stadiums'), exist_ok=True)

        # --- PHASE 1: MASTER STADIUM LISTS ---
        # Changed NFL match_str to "Capacity" for better reliability
        static_tasks = [
            ("https://en.wikipedia.org/wiki/List_of_NBA_arenas", "Capacity", "nba/stadiums/master_arenas.csv", "Master NBA Arenas"),
            ("https://en.wikipedia.org/wiki/List_of_current_Major_League_Baseball_stadiums", "Capacity", "mlb/stadiums/master_stadiums.csv", "Master MLB Stadiums"),
            ("https://en.wikipedia.org/wiki/List_of_current_NFL_stadiums", "Capacity", "nfl/stadiums/master_stadiums.csv", "Master NFL Stadiums")
        ]

        self.log_msg("\n=== PHASE 1: MASTER LISTS ===")
        for url, match_str, rel_path, log_name in static_tasks:
            self.scrape_wiki_table(url, match_str, os.path.join(base_dir, rel_path), log_name)

        # --- PHASE 2: DYNAMIC INDIVIDUAL STADIUMS ---
        self.log_msg("\n=== PHASE 2: DYNAMIC INDIVIDUAL INFOBOXES ===")
        dynamic_tasks = []
        
        # NBA Arenas
        nba_path = os.path.join(base_dir, 'nba', 'stadiums', 'master_arenas.csv')
        if os.path.exists(nba_path):
            df = pd.read_csv(nba_path)
            if 'Arena' in df.columns:
                for arena in df['Arena'].dropna():
                    c_name = self.clean_name(arena)
                    url = f"https://en.wikipedia.org/wiki/{c_name.replace(' ', '_')}"
                    fname = c_name.replace(' ', '_').lower()
                    dynamic_tasks.append((url, "Capacity", f"nba/stadiums/{fname}_infobox.csv", f"NBA Arena: {c_name}"))

        # MLB Stadiums
        mlb_path = os.path.join(base_dir, 'mlb', 'stadiums', 'master_stadiums.csv')
        if os.path.exists(mlb_path):
            df = pd.read_csv(mlb_path)
            if 'Name' in df.columns:
                for stadium in df['Name'].dropna():
                    c_name = self.clean_name(stadium)
                    url = f"https://en.wikipedia.org/wiki/{c_name.replace(' ', '_')}"
                    fname = c_name.replace(' ', '_').lower()
                    dynamic_tasks.append((url, "Capacity", f"mlb/stadiums/{fname}_infobox.csv", f"MLB Stadium: {c_name}"))

        # NFL Stadiums
        nfl_path = os.path.join(base_dir, 'nfl', 'stadiums', 'master_stadiums.csv')
        if os.path.exists(nfl_path):
            df = pd.read_csv(nfl_path)
            if 'Name' in df.columns:
                for stadium in df['Name'].dropna():
                    c_name = self.clean_name(stadium)
                    url = f"https://en.wikipedia.org/wiki/{c_name.replace(' ', '_')}"
                    fname = c_name.replace(' ', '_').lower()
                    dynamic_tasks.append((url, "Capacity", f"nfl/stadiums/{fname}_infobox.csv", f"NFL Stadium: {c_name}"))

        total_dynamic = len(dynamic_tasks)
        if total_dynamic > 0:
            self.log_msg(f"Generated {total_dynamic} individual stadium tasks. Processing...")
            for idx, task in enumerate(dynamic_tasks):
                url, match_str, rel_path, log_name = task
                self.scrape_wiki_table(url, match_str, os.path.join(base_dir, rel_path), log_name)
                self.progress['value'] = ((idx + 1) / total_dynamic) * 100
        else:
            self.log_msg("No dynamic tasks generated (Master files might be missing).", "WARN")

        self.log_msg("\n=== COMPLETE: ARENA SCRAPING TASKS FINISHED ===", "PASS")
        self.start_btn.config(state=tk.NORMAL)

if __name__ == "__main__":
    root = tk.Tk()
    app = SportsScraperApp(root)
    root.mainloop()