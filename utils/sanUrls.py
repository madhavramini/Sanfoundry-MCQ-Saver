import re
import logging
from typing import List, Set
from urllib.parse import urlparse, urljoin

import cloudscraper
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
    ]


class Urls:
    """Fetch and validate URLs from Sanfoundry MCQ listing pages"""

    def __init__(self):
        self.scraper = cloudscraper.create_scraper()
        self.url = self._get_url_from_user()
        self.url_list: List[str] = []
        self.seen_urls: Set[str] = set()

        # Compile regex patterns
        self.url_pattern = re.compile(
            r"^(https?://)?(www\.)?[a-z0-9]+([\-.][a-z0-9]+)*\.[a-z]{2,}(:[0-9]{1,5})?(/.*)?$",
            re.IGNORECASE
        )
        self.excluded_patterns = [re.compile(pattern) for pattern in Config.EXCLUDED_PATTERNS]

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
        Fetch all MCQ URLs from the listing page

        Returns:
            List of valid MCQ URLs
        """
        try:
            logger.info(f"Fetching URLs from: {self.url}")

            # Fetch page content
            response = self.scraper.get(self.url, timeout=Config.REQUEST_TIMEOUT)
            response.raise_for_status()

            soup = bs(response.content, "lxml")

            # Find main content area
            content = soup.find("div", {"class": "inside-article"})
            if not content:
                content = soup.find("div", {"class": "entry-content"})
            if not content:
                logger.warning("Could not find content area, searching entire page")
                content = soup

            # Find all URL tables/lists
            url_tables = content.find_all('table')

            if not url_tables:
                logger.warning("No tables found, trying to find links directly")
                url_tables = [content]

            # Extract URLs
            all_links = []
            for table in url_tables:
                links = table.find_all("a", href=True)
                all_links.extend(links)

            logger.info(f"Found {len(all_links)} total links")

            # Process and filter URLs
            print("\nProcessing URLs...")
            for link in tqdm(all_links, desc="Validating URLs"):
                url = link.get('href', '').strip()

                if self._is_valid_mcq_url(url):
                    normalized_url = self._normalize_url(url)
                    if normalized_url and normalized_url not in self.seen_urls:
                        self.url_list.append(normalized_url)
                        self.seen_urls.add(normalized_url)

            logger.info(f"Found {len(self.url_list)} valid MCQ URLs")
            print(f"\n✓ Found {len(self.url_list)} valid MCQ URLs")

            return self.url_list

        except Exception as e:
            logger.error(f"Error fetching URLs: {e}", exc_info=True)
            print(f"\n✗ Error: {e}")
            return []

    def _is_valid_mcq_url(self, url: str) -> bool:
        """
        Check if URL is a valid MCQ URL

        Args:
            url: URL to validate

        Returns:
            True if valid, False otherwise
        """
        if not url or not isinstance(url, str):
            return False

        # Must match URL pattern
        if not self.url_pattern.match(url):
            return False

        # Must be from valid domain
        try:
            parsed = urlparse(url)
            if parsed.netloc and parsed.netloc.lower() not in Config.VALID_DOMAINS:
                return False
        except Exception:
            return False

        # Must not match excluded patterns
        for pattern in self.excluded_patterns:
            if pattern.match(url):
                return False

        # Additional filters
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
        ]):
            return False

        return True

    @staticmethod
    def _normalize_url(url: str) -> str:
        """
        Normalize URL (remove fragments, ensure https, etc.)

        Args:
            url: URL to normalize

        Returns:
            Normalized URL
        """
        try:
            # Parse URL
            parsed = urlparse(url)

            # Ensure https
            scheme = 'https'

            # Remove fragment and query params
            normalized = f"{scheme}://{parsed.netloc}{parsed.path}"

            # Remove trailing slash
            normalized = normalized.rstrip('/')

            return normalized
        except Exception as e:
            logger.error(f"Error normalizing URL {url}: {e}")
            return url
