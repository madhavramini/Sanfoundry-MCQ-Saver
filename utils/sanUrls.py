import re
import logging
import time
from typing import List, Set, Optional
from urllib.parse import urlparse, urljoin

from bs4 import BeautifulSoup as bs
from tqdm import tqdm

logger = logging.getLogger(__name__)


class Config:
    """Configuration constants"""
    REQUEST_TIMEOUT = 30
    VALID_DOMAINS = ['sanfoundry.com', 'www.sanfoundry.com']

    # These URL patterns are never MCQ pages — skip them outright
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

    # Patterns that indicate a page is an MCQ question page (not a listing)
    MCQ_URL_INDICATORS = [
        r'.*-questions-answers.*',
        r'.*-mcqs?-.*',
        r'.*-mcq-.*',
        r'.*-multiple-choice.*',
        r'.*-objective-questions.*',
    ]

    # Content container selectors tried in order
    CONTENT_SELECTORS = [
        ("div", {"class": "entry-content"}),
        ("div", {"class": "inside-article"}),
        ("article", {}),
        ("div", {"id": "content"}),
        ("main", {}),
    ]

    # Maximum depth for recursive crawl of intermediate listing pages
    MAX_CRAWL_DEPTH = 3


class Urls:
    """Fetch and validate URLs from Sanfoundry MCQ listing pages with recursive crawling"""

    def __init__(self, page=None):
        self.page = page
        self.url = self._get_url_from_user()
        self.url_list: List[str] = []
        self.seen_urls: Set[str] = set()
        self.crawled_pages: Set[str] = set()

        # Derive semantic subject keywords from the listing URL.
        # e.g. "1000-database-management-system-questions-answers" → ["database", "management", "system"]
        self._subject_keywords = self._derive_subject_keywords(self.url)

        # Compile regex patterns
        self.url_pattern = re.compile(
            r"^https?://(www\.)?sanfoundry\.com/.+$",
            re.IGNORECASE
        )
        self.excluded_patterns = [re.compile(p) for p in Config.EXCLUDED_PATTERNS]
        self.mcq_indicators = [re.compile(p, re.IGNORECASE) for p in Config.MCQ_URL_INDICATORS]

    @staticmethod
    def _derive_subject_keywords(url: str) -> List[str]:
        """
        Derive semantic subject keywords from the listing URL path.

        Strips:
          - Leading numeric prefix  (e.g. "1000-", "500-")
          - Common trailing suffixes (e.g. "-questions-answers", "-mcq-...")

        Examples:
          .../1000-database-management-system-questions-answers/
              → ["database", "management", "system"]
          .../computer-network-questions-answers/
              → ["computer", "network"]
          .../operating-system-questions-answers/
              → ["operating", "system"]
        """
        try:
            path = urlparse(url).path.strip('/')

            # Strip trailing suffixes
            for suffix in [
                '-questions-answers',
                '-mcq-multiple-choice-questions',
                '-interview-questions',
                '-objective-questions',
            ]:
                if path.endswith(suffix):
                    path = path[: -len(suffix)]
                    break

            # Strip leading numeric prefix like "1000-", "500-", "250-"
            path = re.sub(r'^\d+-', '', path)

            # Split into individual keywords
            keywords = [k for k in path.lower().split('-') if k]
            return keywords
        except Exception:
            return []

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

    def _fetch_html(self, url: str) -> Optional[str]:
        """
        Fetch the HTML content of a URL using the browser page.

        Args:
            url: URL to fetch

        Returns:
            HTML content as string, or None on failure
        """
        try:
            if not self.page:
                from DrissionPage import ChromiumPage, ChromiumOptions
                co = ChromiumOptions().set_local_port(9333)
                co.no_imgs(True)
                self.page = ChromiumPage(co)

            self.page.get(url)

            # Wait for Cloudflare bypass if needed
            for _ in range(10):
                if "Just a moment" not in self.page.title:
                    break
                time.sleep(1)

            return self.page.html
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            return None

    def _extract_links_from_html(self, html: str, base_url: str) -> List[str]:
        """
        Extract all absolute Sanfoundry links from a page's content area.

        Args:
            html: Raw HTML content string
            base_url: Base URL for resolving relative hrefs

        Returns:
            List of absolute URL strings found on the page
        """
        soup = bs(html, "lxml")
        parsed_base = urlparse(base_url)
        base = f"{parsed_base.scheme}://{parsed_base.netloc}"

        # Try content selectors in priority order
        content = None
        for tag, attrs in Config.CONTENT_SELECTORS:
            content = soup.find(tag, attrs) if attrs else soup.find(tag)
            if content:
                logger.debug(f"Found content area: <{tag} {attrs}> on {base_url}")
                break

        if not content:
            logger.warning(f"No known content area found on {base_url} — searching full page")
            content = soup

        links = []
        for link in content.find_all("a", href=True):
            href = link.get("href", "").strip()
            if not href or href.startswith("#"):
                continue
            absolute = urljoin(base, href)
            links.append(absolute)

        return links

    def _is_on_topic(self, url: str) -> bool:
        """
        Check whether a URL is on the same topic as the listing page.

        Uses the derived subject keywords: at least half of them must appear
        in the URL path (joined), providing resilience to plural/abbreviation
        variants (e.g. "network" matching "networks").

        Args:
            url: Absolute URL to test

        Returns:
            True if on-topic, False otherwise
        """
        if not self._subject_keywords:
            # No keywords — allow everything (can't filter)
            return True

        try:
            path = urlparse(url).path.lower()

            # Count how many subject keywords appear in the path
            matches = sum(1 for kw in self._subject_keywords if kw.rstrip('s') in path)

            # Require at least half the keywords to match
            threshold = max(1, len(self._subject_keywords) // 2)
            return matches >= threshold
        except Exception:
            return False

    def _is_valid_mcq_url(self, url: str) -> bool:
        """
        Check if URL is a valid MCQ page URL for the current subject.

        Args:
            url: Fully-resolved absolute URL to validate

        Returns:
            True if valid MCQ page URL, False otherwise
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

            # Must not be the listing page itself
            url_path = parsed.path.strip('/')
            listing_path = urlparse(self.url).path.strip('/')
            if url_path == listing_path:
                return False

        except Exception:
            return False

        # Must not match excluded patterns
        for pattern in self.excluded_patterns:
            if pattern.match(url):
                return False

        # Must not contain generic excluded keywords
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
            'feed',
            'xmlrpc',
        ]):
            return False

        # Must be on the same topic
        if not self._is_on_topic(url):
            return False

        # Must look like an MCQ page (contains an MCQ-style slug)
        path = urlparse(url).path.lower()
        is_mcq = any(p.search(path) for p in self.mcq_indicators)
        return is_mcq

    def _is_crawlable_listing(self, url: str) -> bool:
        """
        Check if a URL looks like an intermediate listing page worth recursively crawling.

        These are Sanfoundry on-topic pages that link to MCQ pages but are not
        themselves MCQ pages — e.g. section index pages.

        Args:
            url: Absolute URL to test

        Returns:
            True if worth crawling recursively
        """
        if not url or not isinstance(url, str):
            return False

        if not self.url_pattern.match(url):
            return False

        # Must be on the same topic
        if not self._is_on_topic(url):
            return False

        # Must not be a known-bad URL
        for pattern in self.excluded_patterns:
            if pattern.match(url):
                return False

        url_lower = url.lower()
        if any(keyword in url_lower for keyword in [
            'privacy-policy', 'terms-of-service', 'contact', 'about',
            'login', 'register', 'wp-admin', 'wp-content', 'sitemap',
        ]):
            return False

        # Must not be the starting listing page itself
        try:
            if urlparse(url).path.strip('/') == urlparse(self.url).path.strip('/'):
                return False
        except Exception:
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

    def _crawl_page(self, url: str, depth: int) -> None:
        """
        Recursively crawl a page for MCQ URLs and intermediate listing pages.

        Args:
            url: Page URL to crawl
            depth: Current recursion depth (stops at Config.MAX_CRAWL_DEPTH)
        """
        normalized = self._normalize_url(url)

        if normalized in self.crawled_pages:
            return
        if depth > Config.MAX_CRAWL_DEPTH:
            logger.debug(f"Max crawl depth reached at: {url}")
            return

        self.crawled_pages.add(normalized)
        logger.info(f"Crawling (depth={depth}): {url}")

        html = self._fetch_html(url)
        if not html:
            return

        all_links = self._extract_links_from_html(html, url)
        logger.info(f"  Found {len(all_links)} raw links on page")

        pending_listings: List[str] = []

        for link in all_links:
            # Skip the root listing page
            if self._normalize_url(link) == self._normalize_url(self.url):
                continue

            if self._is_valid_mcq_url(link):
                norm = self._normalize_url(link)
                if norm not in self.seen_urls:
                    self.url_list.append(norm)
                    self.seen_urls.add(norm)
            elif depth < Config.MAX_CRAWL_DEPTH and self._is_crawlable_listing(link):
                norm = self._normalize_url(link)
                if norm not in self.crawled_pages:
                    pending_listings.append(norm)

        # Recurse into intermediate listing pages
        for listing_url in pending_listings:
            self._crawl_page(listing_url, depth + 1)

    def get_urls(self) -> List[str]:
        """
        Fetch all MCQ URLs from the listing page, recursively following
        intermediate listing pages when needed.

        Returns:
            List of valid MCQ URLs
        """
        try:
            logger.info(f"Fetching URLs from: {self.url}")
            logger.info(f"Subject keywords: {self._subject_keywords}")

            print("\nProcessing URLs...")
            self._crawl_page(self.url, depth=0)

            logger.info(f"Found {len(self.url_list)} valid MCQ URLs")
            print(f"\n[SUCCESS] Found {len(self.url_list)} valid MCQ URLs")

            return self.url_list

        except Exception as e:
            logger.error(f"Error fetching URLs: {e}", exc_info=True)
            print(f"\n[ERROR] Error: {e}")
            return []
