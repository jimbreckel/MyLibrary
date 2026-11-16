import json
import re
from pathlib import Path
from typing import Tuple, Optional, Dict, List

# Define default path for magazines.json (inside orgperiodicals/ or parent)
_PKG_CONFIG_PATH = Path(__file__).resolve().parent / "magazines.json"
DEFAULT_CONFIG_PATH = _PKG_CONFIG_PATH if _PKG_CONFIG_PATH.exists() else (Path(__file__).resolve().parent.parent / "magazines.json")

class PeriodicalClassifier:
    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self.config = self.load_config()
        self.patterns_cache = self.build_patterns_cache()

    def load_config(self) -> List[Dict]:
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Error loading magazines.json: {e}")
        return []

    def save_config(self):
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            print(f"Error saving magazines.json: {e}")

    def build_patterns_cache(self) -> List[Tuple[str, str, bool, re.Pattern]]:
        """
        Builds a sorted list of patterns by length descending to prevent shorter
        patterns (like 'Times') from eagerly matching before longer ones (like 'Angling Times').
        Returns list of (bucket_name, pattern_str, pub_year_avail, compiled_regex)
        """
        generic_skip = {
            'issue', 'vol', 'volume', 'no', 'nr', 'heft', 'ausgabe', 'magazine', 'magazin', 'truepdf', 'pdf',
            'extract', 'calendar', 'special', 'edition', 'spring', 'summer', 'autumn', 'fall', 'winter',
            'annual', 'book', 'series', 'guide', 'manual', 'new', 'the', 'old', 'big', 'top', 'hot', 'all',
            'one', 'now', 'best', 'free', 'world', 'daily', 'weekly', 'monthly',
            'computer', 'computers', 'pc', 'mac', 'linux', 'windows', 'games', 'game', 'gaming',
            'science', 'history', 'home', 'music', 'art', 'arts', 'photo', 'photography', 'cars', 'car',
            'auto', 'sport', 'sports', 'tech', 'technology', 'health', 'food', 'travel', 'nature',
            'garden', 'gardening', 'fishing', 'hunting', 'aviation', 'military', 'guitar', 'audio',
            'video', 'film', 'movie', 'movies', 'design', 'fashion', 'style', 'fitness', 'running',
            'cycling', 'bike', 'yacht', 'boating', 'craft', 'wood', 'trains', 'rail', 'model', 'radio',
            'electronics', 'coding', 'python', 'java', 'web', 'digital', 'modern', 'american', 'british',
            'australian', 'international', 'national', 'popular', 'practical', 'classic', 'retro', 'future',
            'info',
        }
        bad_pattern_re = re.compile(
            r'(?:\b(?:no|nr|issue|iss|vol|volume|heft|ausgabe)\b|#\d|\b(?:19\d\d|20\d\d)\b|\s+(?:0[1-9]|1[0-2]|\d{3})$)',
            re.IGNORECASE,
        )
        raw_patterns = []
        for entry in self.config:
            bucket = entry.get("bucket", "Unorganized")
            pub_year = entry.get("publication_year_available", True)
            for pat in entry.get("patterns", []):
                p_clean = (pat or "").strip()
                if not p_clean or p_clean.lower() in generic_skip:
                    continue
                if bad_pattern_re.search(p_clean) and p_clean.upper() != "INFO 64":
                    continue
                raw_patterns.append((bucket, p_clean, pub_year))
        
        # Sort patterns by length descending
        raw_patterns.sort(key=lambda x: len(x[1]), reverse=True)
        
        cache = []
        for bucket, pat, pub_year in raw_patterns:
            if not pat:
                continue
            # Check if pattern is already formatted as a regex or needs boundaries
            if pat.startswith('^') or pat.endswith('$') or '\\b' in pat:
                regex_str = pat
            else:
                start_boundary = r'\b' if pat[0].isalnum() else ''
                end_boundary = r'\b' if pat[-1].isalnum() else ''
                regex_str = start_boundary + re.escape(pat) + end_boundary
            
            try:
                compiled = re.compile(regex_str, re.IGNORECASE)
                cache.append((bucket, pat, pub_year, compiled))
            except re.error:
                # If compilation fails, use simple case-insensitive substring match
                pass
        return cache

    def match_filename(self, filename: str) -> Optional[Tuple[str, str, bool]]:
        """
        Attempts to match the filename against known patterns in magazines.json.
        Returns: Tuple[bucket_name, matched_pattern, publication_year_available] or None
        """
        for bucket, pat, pub_year, compiled in self.patterns_cache:
            if compiled.search(filename):
                return bucket, pat, pub_year
        return None

    def classify_new_title(self, title: str, filepath: Path, has_year: bool) -> str:
        """
        Classifies a new title into one of the 4 buckets using heuristics.
        Appends the title to the bucket in magazines.json and saves the config.
        """
        title = title.strip() if title else ""
        if not title:
            # Fallback to a default bucket if title is empty
            return "Monthly and Special Interest Magazines"

        # Validate pattern to prevent junk/generic entries
        title_lower = title.lower()
        
        # Validation rules
        months = {
            'january', 'february', 'march', 'april', 'may', 'june', 'july', 'august', 'september', 'october', 'november', 'december',
            'jan', 'feb', 'mar', 'apr', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'
        }
        generic_words = {
            'issue', 'vol', 'volume', 'no', 'magazine', 'truepdf', 'pdf', 'extract', 'calendar', 'special', 'edition',
            'spring', 'summer', 'autumn', 'fall', 'winter', 'annual', 'book', 'series', 'guide', 'manual',
            'new', 'the', 'old', 'big', 'top', 'hot', 'all', 'one', 'now', 'best', 'free', 'world', 'daily', 'weekly', 'monthly'
        }
        
        is_valid = True
        if title_lower.isdigit() or re.match(r'^[\d\s\W_]+$', title_lower):
            is_valid = False
        elif len(title_lower) < 3:
            is_valid = False
        elif title_lower in months:
            is_valid = False
        elif title_lower in generic_words:
            is_valid = False
            
        title_lower = title.lower()
        filepath_lower = str(filepath).lower()
        
        # Heuristics
        # 1. Adult Periodicals
        if 'adult' in filepath_lower or 'adult' in title_lower or any(w in title_lower for w in ['playboy', 'penthouse', 'forum', 'nude', 'sexy']):
            bucket = "Adult Periodicals"
        # 2. Daily Newspapers
        elif any(w in title_lower for w in [
            'daily', 'news', 'times', 'post', 'journal', 'tribune', 'star', 'sun',
            'express', 'mirror', 'globe', 'mail', 'telegraph', 'observer', 'guardian',
            'today', 'welt', 'monde', 'parisien', 'provence', 'examiner', 'chronicle'
        ]):
            bucket = "Daily Newspapers"
        # 3. Weekly Gossip and Lifestyle Magazines
        elif not has_year or any(w in title_lower for w in [
            'bella', 'best', 'closer', 'fabulous', 'heat', 'chat', 'soap', 'hello',
            'weekly', 'gossip', 'people', 'chic', 'woman', 'womans'
        ]):
            bucket = "Weekly Gossip and Lifestyle Magazines"
        # 4. Monthly and Special Interest Magazines
        else:
            bucket = "Monthly and Special Interest Magazines"
            
        # Add to config if it's a valid pattern
        if is_valid:
            for entry in self.config:
                if entry.get("bucket") == bucket:
                    if title not in entry["patterns"]:
                        entry["patterns"].append(title)
                        entry["patterns"].sort() # Keep patterns sorted alphabetically
                        self.save_config()
                        # Rebuild pattern cache so the new pattern is immediately active
                        self.patterns_cache = self.build_patterns_cache()
                    break
                
        return bucket
