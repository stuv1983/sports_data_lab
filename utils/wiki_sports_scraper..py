import os
import io
import re
import threading
import tkinter as tk
from tkinter import filedialog, ttk, scrolledtext
import requests
import pandas as pd

class SportsScraperApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Sports Data Lab - Awards Scraper")
        self.root.geometry("850x650")
        
        self.save_directory = tk.StringVar()
        
        # --- GUI Setup ---
        dir_frame = tk.Frame(root)
        dir_frame.pack(pady=10, fill=tk.X, padx=10)
        
        tk.Label(dir_frame, text="Save Location:").pack(side=tk.LEFT)
        tk.Entry(dir_frame, textvariable=self.save_directory, width=65, state='readonly').pack(side=tk.LEFT, padx=10)
        tk.Button(dir_frame, text="Browse...", command=self.choose_directory).pack(side=tk.LEFT)
        
        control_frame = tk.Frame(root)
        control_frame.pack(pady=5, fill=tk.X, padx=10)
        self.start_btn = tk.Button(control_frame, text="Start Award Scrape", command=self.start_scraping, bg="green", fg="white")
        self.start_btn.pack(side=tk.LEFT)
        
        self.progress = ttk.Progressbar(root, orient=tk.HORIZONTAL, length=830, mode='determinate')
        self.progress.pack(pady=10, padx=10)
        
        self.log_area = scrolledtext.ScrolledText(root, wrap=tk.WORD, width=100, height=30, state='disabled', bg="black", fg="lightgreen")
        self.log_area.pack(pady=10, padx=10)
        
        self.log_msg("System ready. Select your 'allSports' folder and click Start.")

    def choose_directory(self):
        folder_selected = filedialog.askdirectory()
        if folder_selected:
            self.save_directory.set(folder_selected)
            self.log_msg(f"Target directory set to: {folder_selected}")

    def log_msg(self, message, msg_type="INFO"):
        """Safely write to the log area from any thread."""
        self.log_area.config(state='normal')
        
        if msg_type == "ERROR":
            self.log_area.insert(tk.END, f"[{msg_type}] {message}\n", "error")
            self.log_area.tag_config("error", foreground="red")
        elif msg_type == "PASS":
            self.log_area.insert(tk.END, f"[{msg_type}] {message}\n", "pass")
            self.log_area.tag_config("pass", foreground="cyan")
        else:
            self.log_area.insert(tk.END, f"[{msg_type}] {message}\n")
            
        self.log_area.see(tk.END)
        self.log_area.config(state='disabled')
        self.root.update_idletasks()

    def start_scraping(self):
        target_dir = self.save_directory.get()
        if not target_dir:
            self.log_msg("ERROR: Please select a save location first.", "ERROR")
            return
            
        self.start_btn.config(state=tk.DISABLED)
        self.progress['value'] = 0
        self.log_msg("Starting Awards Scraping Sequence...")
        
        thread = threading.Thread(target=self.run_scraper, args=(target_dir,))
        thread.daemon = True
        thread.start()

    # ==========================================
    # Scraping Engine
    # ==========================================
    
    def fetch_html_with_headers(self, url):
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
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
                
                # EDGE CASE 1: Flatten MultiIndex columns (if the table has grouped headers)
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = ['_'.join(col).strip() for col in df.columns.values]
                
                # EDGE CASE 2: Clean up Wikipedia citation brackets like [a] or [12]
                df = df.replace(r'\[.*?\]', '', regex=True)
                
                # EDGE CASE 3: Remove special Wikipedia symbols (*, †, ‡, ^) from text columns
                for col in df.select_dtypes(include=['object']):
                    # Using regex to strip the specific symbols, then stripping leading/trailing whitespace
                    df[col] = df[col].astype(str).str.replace(r'[*†‡^]', '', regex=True).str.strip()
                
                # EDGE CASE 4: Drop repeating header rows (Wikipedia repeats headers on long tables)
                first_col = df.columns[0]
                df = df[df[first_col] != first_col]
                
                # EDGE CASE 5: Drop messy visual/reference columns
                cols_to_drop = [c for c in df.columns if 'Coordinates' in c or 'Image' in c or 'Ref' in c]
                if cols_to_drop:
                    df = df.drop(columns=cols_to_drop)
                    
                df.to_csv(save_path, index=False, encoding='utf-8-sig') # Enforced UTF-8 for Excel safety
                self.log_msg(f"Saved {len(df)} clean rows for {log_name}", "PASS")
                return True
            else:
                self.log_msg(f"Table '{match_str}' not found on {log_name}", "ERROR")
                return False
        except Exception as e:
            self.log_msg(f"Failed on {log_name}: {str(e)}", "ERROR")
            return False

    # ==========================================
    # Execution Sequence
    # ==========================================

    def run_scraper(self, base_dir):
        # Create subdirectories specifically for awards to keep data clean
        os.makedirs(os.path.join(base_dir, 'nfl', 'awards'), exist_ok=True)
        os.makedirs(os.path.join(base_dir, 'nba', 'awards'), exist_ok=True)

        award_tasks = [
            # NFL AWARDS
            ("https://en.wikipedia.org/wiki/NFL_Honors", "Season", "nfl/awards/nfl_honors.csv", "NFL Honors"),
            ("https://en.wikipedia.org/wiki/AP_NFL_Most_Valuable_Player", "Season", "nfl/awards/mvp.csv", "NFL MVP"),
            ("https://en.wikipedia.org/wiki/AP_NFL_Offensive_Player_of_the_Year", "Season", "nfl/awards/offensive_poy.csv", "NFL OPOY"),
            ("https://en.wikipedia.org/wiki/AP_NFL_Defensive_Player_of_the_Year", "Season", "nfl/awards/defensive_poy.csv", "NFL DPOY"),
            ("https://en.wikipedia.org/wiki/AP_NFL_Rookie_of_the_Year", "Season", "nfl/awards/rookie_of_year.csv", "NFL ROY"),
            ("https://en.wikipedia.org/wiki/AP_NFL_Comeback_Player_of_the_Year", "Season", "nfl/awards/comeback_poy.csv", "NFL Comeback POY"),
            ("https://en.wikipedia.org/wiki/AP_NFL_Coach_of_the_Year", "Season", "nfl/awards/coach_of_year.csv", "NFL Coach of the Year"),
            ("https://en.wikipedia.org/wiki/AP_NFL_Assistant_Coach_of_the_Year", "Season", "nfl/awards/assistant_coach.csv", "NFL Assistant Coach"),
            ("https://en.wikipedia.org/wiki/List_of_Pro_Football_Hall_of_Fame_inductees", "Year inducted", "nfl/awards/hall_of_fame.csv", "NFL Hall of Fame"),

            # NBA AWARDS & TROPHIES
            ("https://en.wikipedia.org/wiki/List_of_NBA_awards", "Award", "nba/awards/list_of_awards.csv", "List of NBA Awards"),
            ("https://en.wikipedia.org/wiki/Larry_O%27Brien_Championship_Trophy", "Year", "nba/awards/larry_obrien.csv", "Larry O'Brien Trophy"),
            ("https://en.wikipedia.org/wiki/Walter_A._Brown_Trophy", "Year", "nba/awards/walter_a_brown.csv", "Walter A. Brown Trophy"),
            ("https://en.wikipedia.org/wiki/Maurice_Podoloff_Trophy", "Season", "nba/awards/maurice_podoloff.csv", "Maurice Podoloff Trophy"),
            ("https://en.wikipedia.org/wiki/NBA_Cup", "Season", "nba/awards/nba_cup.csv", "NBA Cup"),
            ("https://en.wikipedia.org/wiki/All-NBA_Team", "Season", "nba/awards/all_nba_team.csv", "All-NBA Team"),
            ("https://en.wikipedia.org/wiki/NBA_All-Rookie_Team", "Season", "nba/awards/all_rookie_team.csv", "NBA All-Rookie Team"),
            ("https://en.wikipedia.org/wiki/NBA_All-Defensive_Team", "Season", "nba/awards/all_defensive_team.csv", "NBA All-Defensive Team"),
            ("https://en.wikipedia.org/wiki/NBA_All-Star_Game_Most_Valuable_Player", "Year", "nba/awards/all_star_mvp.csv", "NBA All-Star MVP"),
            ("https://en.wikipedia.org/wiki/NBA_Rookie_of_the_Year", "Season", "nba/awards/rookie_of_year.csv", "NBA Rookie of the Year"),
            ("https://en.wikipedia.org/wiki/NBA_Most_Valuable_Player", "Season", "nba/awards/mvp.csv", "NBA MVP"),
            ("https://en.wikipedia.org/wiki/NBA_Coach_of_the_Year", "Season", "nba/awards/coach_of_year.csv", "NBA Coach of the Year"),
            ("https://en.wikipedia.org/wiki/NBA_Finals_Most_Valuable_Player", "Year", "nba/awards/finals_mvp.csv", "NBA Finals MVP"),
            ("https://en.wikipedia.org/wiki/NBA_Conference_Finals_Most_Valuable_Player", "Year", "nba/awards/conference_finals_mvp.csv", "NBA Conference Finals MVP"),
            ("https://en.wikipedia.org/wiki/NBA_Cup#MVP", "Season", "nba/awards/nba_cup_mvp.csv", "NBA Cup MVP"),
            ("https://en.wikipedia.org/wiki/NBA_Defensive_Player_of_the_Year", "Season", "nba/awards/dpoy.csv", "NBA DPOY"),
            ("https://en.wikipedia.org/wiki/NBA_Sixth_Man_of_the_Year", "Season", "nba/awards/sixth_man.csv", "NBA Sixth Man"),
            ("https://en.wikipedia.org/wiki/NBA_Clutch_Player_of_the_Year", "Season", "nba/awards/clutch_poy.csv", "NBA Clutch POY"),
            ("https://en.wikipedia.org/wiki/NBA_Player_of_the_Month_and_Week", "Month", "nba/awards/player_of_month.csv", "NBA Player of the Month/Week"),
            ("https://en.wikipedia.org/wiki/List_of_NBA_All-Stars", "Player", "nba/awards/all_stars_list.csv", "List of NBA All-Stars"),
            ("https://en.wikipedia.org/wiki/List_of_members_of_the_Naismith_Memorial_Basketball_Hall_of_Fame", "Inductee", "nba/awards/naismith_hof.csv", "Naismith HOF")
        ]

        self.log_msg("\n=== EXECUTING AWARDS SCRAPE ===")
        total_tasks = len(award_tasks)
        
        for idx, task in enumerate(award_tasks):
            url, match_str, rel_path, log_name = task
            save_path = os.path.join(base_dir, rel_path)
            
            self.scrape_wiki_table(url, match_str, save_path, log_name)
            
            # Update progress bar
            self.progress['value'] = ((idx + 1) / total_tasks) * 100

        self.log_msg("\n=== SUCCESS: ALL AWARD SCRAPING TASKS COMPLETED ===", "PASS")
        self.start_btn.config(state=tk.NORMAL)

if __name__ == "__main__":
    root = tk.Tk()
    app = SportsScraperApp(root)
    root.mainloop()