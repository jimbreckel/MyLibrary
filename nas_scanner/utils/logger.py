import logging
import os
import sys

class LineLimitingFileHandler(logging.FileHandler):
    """
    A file log handler that keeps the log file size limited to a maximum number of lines.
    When the line count exceeds `max_lines`, it prunes the file to keep only the most recent
    80% of lines (to avoid pruning on every single log write).
    """
    def __init__(self, filename, max_lines=100000, encoding='utf-8', delay=False, errors=None):
        filename = os.path.abspath(filename)
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        super().__init__(filename, mode='a', encoding=encoding, delay=delay, errors=errors)
        self.max_lines = max_lines
        self._line_count = self._count_lines()

    def _count_lines(self):
        if not os.path.exists(self.baseFilename):
            return 0
        try:
            with open(self.baseFilename, 'r', encoding=self.encoding, errors='ignore') as f:
                return sum(1 for _ in f)
        except Exception:
            return 0

    def emit(self, record):
        try:
            msg = self.format(record)
            lines_to_write = msg.count('\n') + 1
            
            super().emit(record)
            
            self._line_count += lines_to_write
            
            if self._line_count > self.max_lines:
                self.close()
                self.prune_log()
                self._line_count = self._count_lines()
        except Exception:
            self.handleError(record)

    def prune_log(self):
        try:
            if os.path.exists(self.baseFilename):
                with open(self.baseFilename, 'r', encoding=self.encoding, errors='ignore') as f:
                    lines = f.readlines()
                keep_count = int(self.max_lines * 0.8)
                if len(lines) > keep_count:
                    with open(self.baseFilename, 'w', encoding=self.encoding, errors='ignore') as f:
                        f.writelines(lines[-keep_count:])
        except Exception:
            pass

def setup_logging(log_file='scanner.log', level=logging.INFO):
    """
    Configures root logger to write to both standard output and a line-limited file handler.
    Also captures python warnings and redirects them to the logging system.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Clear existing handlers to avoid duplicates
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s: %(message)s')
    
    # File Handler
    file_handler = LineLimitingFileHandler(log_file, max_lines=100000, encoding='utf-8')
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    # Stream Handler (console)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)
    
    # Redirect warnings
    logging.captureWarnings(True)
    
    return root_logger
