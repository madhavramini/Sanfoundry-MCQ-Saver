import os
import sys
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from xhtml2pdf import pisa
from bs4 import BeautifulSoup as bs
from tqdm import tqdm

# Fix for pypdf imports - handle both old and new versions
try:
    from pypdf import PdfMerger, PdfReader
except ImportError:
    try:
        from pypdf import PdfWriter as PdfMerger, PdfReader
    except ImportError:
        # Fallback to PyPDF2 if pypdf not available
        from PyPDF2 import PdfMerger, PdfReader

from utils.sanCleaner import Cleaner
from utils.sanUrls import Urls

class _TqdmLoggingHandler(logging.StreamHandler):
    """Logging handler that writes via tqdm.write() to avoid breaking progress bars."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            tqdm.write(msg)
            self.flush()
        except Exception:
            self.handleError(record)


# Configure logging
# Console output is routed through tqdm.write() so the progress bar stays
# in-place when log messages arrive. The file handler is unaffected.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('sanfoundry.log'),
        _TqdmLoggingHandler(),
    ]
)
logger = logging.getLogger(__name__)


class Config:
    """Configuration constants"""
    SF_PATH = Path("SanfoundryFiles")
    MERGED_PATH = Path("Merged_Pdfs")
    PDF_ENCODING = 'utf-8'
    MAX_RETRIES = 3
    REQUEST_TIMEOUT = 30


def check_dir(path: Path) -> None:
    """Create directory if it doesn't exist"""
    path.mkdir(parents=True, exist_ok=True)


def confirm_prompt(question: str) -> bool:
    """Prompt user for yes/no confirmation"""
    while True:
        reply = input(f"{question} (Y/n): ").lower().strip()
        if reply in ("", "y", "yes"):
            return True
        elif reply in ("n", "no"):
            return False
        print("Please enter 'y' or 'n'")


def convert_html_to_pdf(source_html: str, output_filename: Path) -> bool:
    """
    Convert HTML to PDF using xhtml2pdf

    Args:
        source_html: HTML content as string
        output_filename: Path to output PDF file

    Returns:
        True if successful, False otherwise
    """
    try:
        with open(output_filename, "w+b") as result_file:
            pisa_status = pisa.CreatePDF(source_html, dest=result_file)

        if pisa_status.err:
            logger.error(f"PDF conversion failed for {output_filename}")
            return False

        logger.info(f"Successfully created PDF: {output_filename}")
        return True
    except Exception as e:
        logger.error(f"Error creating PDF {output_filename}: {e}")
        return False


class Sanfoundry:
    """Main class for scraping and processing Sanfoundry MCQs"""

    def __init__(self):
        self.mode = self._get_mode()
        self.pdf_options = {
            'encoding': Config.PDF_ENCODING,
            'enable-local-file-access': '',
        }
        self._page = None

        check_dir(Config.SF_PATH)
        check_dir(Config.MERGED_PATH)

        try:
            self._execute_mode()
        except KeyboardInterrupt:
            logger.info("\nOperation cancelled by user")
            sys.exit(0)
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            sys.exit(1)
        finally:
            if self._page is not None:
                try:
                    self._page.quit()
                except Exception:
                    pass

    def _get_browser_page(self):
        """Lazily initialize browser page instance"""
        if self._page is None:
            from DrissionPage import ChromiumPage, ChromiumOptions
            co = ChromiumOptions().set_local_port(9333)
            co.no_imgs(True)
            self._page = ChromiumPage(co)
        return self._page

    @staticmethod
    def _get_mode() -> int:
        """Get operation mode from user"""
        while True:
            try:
                print("\n" + "=" * 50)
                print("SANFOUNDRY MCQ SCRAPER")
                print("=" * 50)
                print("\n0 - Download Single MCQ Page")
                print("1 - Download Multiple MCQ Sets")
                print("2 - Merge Existing PDFs")
                print("\n" + "=" * 50)
 

                mode = int(input("\nEnter mode (0-2): ").strip())
                if mode in (0, 1, 2):
                    return mode
                print("Invalid mode. Please enter 0, 1, or 2.")
            except ValueError:
                print("Invalid input. Please enter a number.")

    def _execute_mode(self) -> None:
        """Execute the selected mode"""
        if self.mode == 1:
            self.auto_scrape()
            self.merge_all_pdfs()
        elif self.mode == 2:
            self.merge_all_pdfs()
        else:
            self.single_scrape()

    def auto_scrape(self) -> None:
        """Automatically scrape multiple MCQ pages"""
        try:
            page = self._get_browser_page()
            url_fetcher = Urls(page=page)
            url_list = url_fetcher.get_urls()

            if not url_list:
                logger.warning("No URLs found to scrape")
                return

            logger.info(f"Found {len(url_list)} URLs to scrape")
            successful = 0
            failed = 0

            for url in tqdm(url_list, desc="Scraping MCQs"):
                if self.scrape(url):
                    successful += 1
                else:
                    failed += 1

            logger.info(f"Scraping complete. Success: {successful}, Failed: {failed}")

        except Exception as e:
            logger.error(f"Error in auto scrape: {e}", exc_info=True)

    def single_scrape(self) -> None:
        """Scrape single MCQ page with option to continue"""
        while True:
            url = input("\nEnter Sanfoundry MCQ URL: ").strip()

            if not url:
                print("URL cannot be empty")
                continue

            self.scrape(url)

            if not confirm_prompt("\nScrape another page?"):
                break

    def scrape(self, url: str) -> bool:
        """
        Scrape a single MCQ page

        Args:
            url: URL to scrape

        Returns:
            True if successful, False otherwise
        """
        for attempt in range(Config.MAX_RETRIES):
            try:
                logger.info(f"Scraping: {url} (attempt {attempt + 1}/{Config.MAX_RETRIES})")

                # Fetch page content using DrissionPage
                page = self._get_browser_page()
                page.get(url)

                # Wait for Cloudflare bypass if needed
                import time
                for _ in range(10):
                    if "Just a moment" not in page.title:
                        break
                    time.sleep(1)

                html_content = page.html

                # Parse and clean HTML
                soup = bs(html_content, "lxml")
                html, has_mathjax = Cleaner().clean(soup, page)

                # Generate filename from URL
                filename = self._generate_filename(url)
                output_path = Config.SF_PATH / f"{filename}.pdf"

                # Set MathJax option if needed
                if has_mathjax:
                    self.pdf_options['window-status'] = 'Rendered'

                # Convert to PDF
                return convert_html_to_pdf(html, output_path)

            except Exception as e:
                logger.error(f"Error scraping {url} (attempt {attempt + 1}): {e}")
                if attempt == Config.MAX_RETRIES - 1:
                    logger.error(f"Failed to scrape {url} after {Config.MAX_RETRIES} attempts")
                    return False

        return False

    @staticmethod
    def _generate_filename(url: str) -> str:
        """Generate safe filename from URL"""
        try:
            # Extract meaningful part from URL
            filename = url.split("/")[3] if len(url.split("/")) > 3 else "mcq"
            # Remove special characters
            filename = "".join(c for c in filename if c.isalnum() or c in ('-', '_'))
            return filename or "mcq"
        except Exception:
            return f"mcq_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    def merge_all_pdfs(self) -> None:
        """Merge all PDF files in the SF_PATH directory"""
        try:
            pdf_files = list(Config.SF_PATH.glob("*.pdf"))

            if not pdf_files:
                logger.warning("No PDF files found to merge")
                print("\nNo PDF files found in the output directory.")
                return

            logger.info(f"Found {len(pdf_files)} PDF files to merge")
            print(f"\nFound {len(pdf_files)} PDF files")

            delete_after = confirm_prompt("Delete individual PDFs after merging?")

            # Handle both PdfMerger and PdfWriter APIs
            merger = PdfMerger()
            failed_files = []

            for pdf_file in tqdm(pdf_files, desc="Merging PDFs"):
                try:
                    # Try new pypdf API first
                    if hasattr(merger, 'append'):
                        merger.append(str(pdf_file))
                    else:
                        # Fallback to old API
                        with open(pdf_file, "rb") as pdf:
                            merger.append(PdfReader(pdf))
                except Exception as e:
                    logger.error(f"Error merging {pdf_file}: {e}")
                    failed_files.append(pdf_file.name)

            if failed_files:
                logger.warning(f"Failed to merge {len(failed_files)} files: {failed_files}")

            # Save merged PDF
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = Config.MERGED_PATH / f"Sanfoundry_Merged_{timestamp}.pdf"

            with open(output_path, "wb") as fout:
                merger.write(fout)

            # Close merger if method exists
            if hasattr(merger, 'close'):
                merger.close()

            logger.info(f"Merged PDF saved to: {output_path}")
            print(f"\n[SUCCESS] Merged PDF saved to: {output_path}")

            if delete_after:
                self._delete_all_pdfs(pdf_files)

        except Exception as e:
            logger.error(f"Error merging PDFs: {e}", exc_info=True)

    @staticmethod
    def _delete_all_pdfs(pdf_files: List[Path]) -> None:
        """Delete specified PDF files"""
        deleted = 0
        for pdf_file in pdf_files:
            try:
                pdf_file.unlink()
                deleted += 1
            except Exception as e:
                logger.error(f"Error deleting {pdf_file}: {e}")

        logger.info(f"Deleted {deleted}/{len(pdf_files)} PDF files")
        print(f"Deleted {deleted} individual PDF files")


def main():
    """Main entry point"""
    try:
        Sanfoundry()
    except Exception as e:
        logger.critical(f"Application crashed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
