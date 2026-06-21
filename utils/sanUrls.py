import re
import logging
import time
from typing import List, Set
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup as bs
from tqdm import tqdm

logger = logging.getLogger(__name__)



class Config:
    """Configuration constants"""
    REQUEST_TIMEOUT = 30
    VALID_DOMAINS = ['sanfoundry.com', 'www.sanfoundry.com']
    EXCLUDED_PATTERNS = [
        r'.*/(best-reference-books|mcq-pdf-download).*',
        r'.*/category/.*',
        r'.*/tag/.*',
        r'.*/author/.*',
        r'.*/certification.*',
        r'.*/internship.*',
        r'.*/jobs.*',
        r'.*/training.*',
        r'.*/contests.*',
        r'.*/bootcamp.*',
        r'.*/programs.*',
        r'.*/videos.*',
        r'.*/c-programming-examples.*',
        r'.*/cpp-programs.*',
        r'.*/java-programming-examples.*',
        r'.*/python-problems-solutions.*',
        r'.*/csharp-programming-examples.*',
        r'.*-algorithms-problems-programming.*',
        r'.*-programming-examples-data-structures.*',
        r'.*/1000-(mathematics|physics|chemistry|biology|class-\d+).*',
        r'.*/quantitative-aptitude.*',
        r'.*/data-interpretation.*',
        r'.*/logical-reasoning.*',
        r'.*/english-grammar.*',
        r'.*/(civil|mechanical|chemical|metallurgical|mining|instrumentation|'
        r'agricultural|marine|mechatronics|aerospace|aeronautical|'
        r'biotechnology)-engineering.*',
    ]
    # Content container selectors tried in order
    CONTENT_SELECTORS = [
        ("div", {"class": "entry-content"}),
        ("div", {"class": "inside-article"}),
        ("article", {}),
        ("div", {"id": "content"}),
        ("main", {}),
    ]


class Urls:
    """Fetch and validate URLs from Sanfoundry MCQ listing pages"""

    def __init__(self, page=None):
        self.page = page
        self.url = self._get_url_from_user()
        self.url_list: List[str] = []
        self.seen_urls: Set[str] = set()

        # Derive a subject keyword from the listing URL path for prefix-filtering.
        # e.g. "computer-network" from "computer-network-questions-answers"
        # e.g. "operating-system" from "operating-system-questions-answers"
        self._subject_prefix = self._derive_subject_prefix(self.url)

        # Compile regex patterns
        self.url_pattern = re.compile(
            r"^https?://(www\.)?sanfoundry\.com/.+$",
            re.IGNORECASE
        )
        self.excluded_patterns = [re.compile(pattern) for pattern in Config.EXCLUDED_PATTERNS]

    @staticmethod
    def _derive_subject_prefix(url: str) -> str:
        """
        Derive a subject keyword prefix from the listing URL.

        Examples:
          .../computer-network-questions-answers/   → "computer-network"
          .../operating-system-questions-answers/   → "operating-system"
          .../data-structures-questions-answers/    → "data-structures"
        """
        try:
            path = urlparse(url).path.strip('/')
            # Strip common suffixes that aren't part of the subject name
            for suffix in ['-questions-answers', '-mcq-multiple-choice-questions',
                           '-interview-questions']:
                if path.endswith(suffix):
                    path = path[:-len(suffix)]
                    break
            return path.lower() if path else ''
        except Exception:
            return ''

    @staticmethod
    def _get_url_from_user() -> str:
        """Get and validate URL from user"""
        while True:
            url = input("\nEnter Sanfoundry MCQ listing URL (where all sections are listed): ").strip()

            if not url:
                print("URL cannot be empty")
                continue

            # Add https:// if not present
            if not url.startswith(('http://', 'https://')):
                url = 'https://' + url

            # Validate domain
            try:
                parsed = urlparse(url)
                if parsed.netloc.lower() not in Config.VALID_DOMAINS:
                    print(f"URL must be from {' or '.join(Config.VALID_DOMAINS)}")
                    continue
                return url
            except Exception as e:
                print(f"Invalid URL: {e}")
                continue

    def get_urls(self) -> List[str]:
        """
        Fetch all MCQ URLs from the listing page.

        Sanfoundry listing pages (e.g. /computer-network-questions-answers/) contain
        a flat list of plain anchor links inside the article body, organized under
        section headings. The links use varied slug patterns such as:
            /computer-networks-mcqs-basics/
            /computer-networks-questions-answers-physical-layer/
            /computer-network-questions-answers-network-topology-set-2/
        There are no <table> elements — links are bare in <p> or <li> tags.

        Returns:
            List of valid MCQ URLs
        """
        try:
            logger.info(f"Fetching URLs from: {self.url}")
            logger.info(f"Subject prefix filter: '{self._subject_prefix}'")

            # Fetch page content using DrissionPage
            if not self.page:
                from DrissionPage import ChromiumPage, ChromiumOptions
                co = ChromiumOptions().set_local_port(9333)
                co.no_imgs(True)
                self.page = ChromiumPage(co)
                self._owns_page = True
            else:
                self._owns_page = False

            self.page.get(self.url)

            # Wait for Cloudflare bypass if needed
            for _ in range(10):
                if "Just a moment" not in self.page.title:
                    break
                time.sleep(1)

            html_content = self.page.html

            if hasattr(self, '_owns_page') and self._owns_page:
                self.page.quit()

            soup = bs(html_content, "lxml")

            # Derive base URL for resolving relative hrefs
            parsed_base = urlparse(self.url)
            base_url = f"{parsed_base.scheme}://{parsed_base.netloc}"

            # Try content selectors in priority order
            content = None
            for tag, attrs in Config.CONTENT_SELECTORS:
                content = soup.find(tag, attrs) if attrs else soup.find(tag)
                if content:
                    logger.info(f"Found content area: <{tag} {attrs}>")
                    break

            if not content:
                # Last resort: search the whole page, but subject-prefix filter
                # will discard nav links pointing to other subjects.
                logger.warning("Could not find any known content area — searching full page")
                content = soup

            # Collect all anchor tags from the content area.
            # Sanfoundry uses plain paragraph/list links, not tables.
            all_links = content.find_all("a", href=True)
            logger.info(f"Found {len(all_links)} total raw links")

            # Process and filter URLs
            print("\nProcessing URLs...")
            for link in tqdm(all_links, desc="Validating URLs"):
                href = link.get('href', '').strip()
                if not href or href.startswith('#'):
                    # Skip empty hrefs and fragment-only TOC anchors
                    continue

                # Resolve relative URLs to absolute before validation
                absolute_url = urljoin(base_url, href)

                # Skip anchor links that just point back to the listing page itself
                if absolute_url.rstrip('/') == self.url.rstrip('/'):
                    continue

                if self._is_valid_mcq_url(absolute_url):
                    normalized_url = self._normalize_url(absolute_url)
                    if normalized_url and normalized_url not in self.seen_urls:
                        self.url_list.append(normalized_url)
                        self.seen_urls.add(normalized_url)

            logger.info(f"Found {len(self.url_list)} valid MCQ URLs")
            print(f"\n[SUCCESS] Found {len(self.url_list)} valid MCQ URLs")

            return self.url_list

        except Exception as e:
            logger.error(f"Error fetching URLs: {e}", exc_info=True)
            print(f"\n[ERROR] Error: {e}")
            return []

    def _is_valid_mcq_url(self, url: str) -> bool:
        """
        Check if URL is a valid MCQ page URL for the current subject.

        Args:
            url: Fully-resolved absolute URL to validate

        Returns:
            True if valid, False otherwise
        """
        if not url or not isinstance(url, str):
            return False

        # Must be a full sanfoundry.com URL
        if not self.url_pattern.match(url):
            return False

        # Must be from valid domain
        try:
            parsed = urlparse(url)
            if parsed.netloc and parsed.netloc.lower() not in Config.VALID_DOMAINS:
                return False

            # Must not be the listing page itself or a fragment of it
            url_path = parsed.path.strip('/')
            listing_path = urlparse(self.url).path.strip('/')
            if url_path == listing_path:
                return False

            # If we have a subject prefix, the URL path must contain it.
            # This prevents nav links (to other subjects) from polluting results
            # when the content selector falls back to the whole page.
            if self._subject_prefix:
                # Allow some flexibility: the slug may use abbreviated forms
                # e.g. "computer-network" matches "computer-networks-mcqs-..."
                # Strip trailing 's' for fuzzy matching
                prefix = self._subject_prefix.rstrip('s')
                if prefix not in url_path.lower():
                    return False

        except Exception:
            return False

        # Must not match excluded patterns
        for pattern in self.excluded_patterns:
            if pattern.match(url):
                return False

        # Additional keyword exclusions
        url_lower = url.lower()
        if any(keyword in url_lower for keyword in [
            'privacy-policy',
            'terms-of-service',
            'contact',
            'about',
            'login',
            'register',
            'wp-admin',
            'wp-content',
            'sitemap',
        ]):
            return False

        return True

    @staticmethod
    def _normalize_url(url: str) -> str:
        """
        Normalize URL: enforce https, strip fragment and query params, strip trailing slash.

        Args:
            url: Fully-resolved absolute URL

        Returns:
            Normalized URL string
        """
        try:
            parsed = urlparse(url)
            normalized = f"https://{parsed.netloc}{parsed.path}"
            return normalized.rstrip('/')
        except Exception as e:
            logger.error(f"Error normalizing URL {url}: {e}")
            return url
