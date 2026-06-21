import base64
import logging
from pathlib import Path
from typing import Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup as bs, Doctype

logger = logging.getLogger(__name__)


class Config:
    """Configuration constants"""
    REQUEST_TIMEOUT = 30
    MAX_IMAGE_WORKERS = 5
    MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10MB
    ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/gif', 'image/webp'}


def add_tags(content: bs, has_mathjax: bool) -> Tuple[str, bool]:
    """
    Add necessary HTML tags and scripts for PDF conversion

    Args:
        content: BeautifulSoup object with cleaned content
        has_mathjax: Whether MathJax rendering is needed

    Returns:
        Tuple of (prettified HTML string, has_mathjax flag)
    """
    html = bs(str(content), "lxml")
    doctype = Doctype('html')
    html.insert(0, doctype)
    head = html.head

    if head is None:
        head = html.new_tag('head')
        html.html.insert(0, head)

    # Add style tag for consistent formatting
    style_tag = html.new_tag('style', type='text/css')
    style_tag.append("""
        * {
            font-family: Arial, Helvetica, sans-serif !important;
        }
        body {
            margin: 20px;
            line-height: 1.6;
        }
        img {
            max-width: 100%;
            height: auto;
        }
        table {
            border-collapse: collapse;
            width: 100%;
        }
        td, th {
            border: 1px solid #ddd;
            padding: 8px;
        }
    """)
    head.append(style_tag)

    # Add MathJax support if needed
    if has_mathjax:
        # Polyfill
        polyfill = html.new_tag('script', src="https://polyfill.io/v3/polyfill.min.js?features=es6")
        head.append(polyfill)

        # MathJax actions
        try:
            mathjax_actions = Path(__file__).parent / "mathjax-actions.js"
            if mathjax_actions.exists():
                head.append(html.new_tag('script', src=f'file:///{mathjax_actions.as_posix()}'))
        except Exception as e:
            logger.warning(f"Could not load mathjax-actions.js: {e}")

        # MathJax configuration
        conf = html.new_tag('script', type="text/x-mathjax-config")
        conf.append("MathJax.Hub.Config({CommonHTML: {scale: 200}});")
        head.append(conf)

        # MathJax library
        head.append(html.new_tag(
            'script',
            id="MathJax-script",
            attrs={'async': ''},
            src="https://cdnjs.cloudflare.com/ajax/libs/mathjax/2.7.7/MathJax.js?config=TeX-MML-AM_CHTML"
        ))

    return html.prettify(), has_mathjax


def fetch_image_base64(img_url: str) -> Optional[str]:
    """
    Fetch image and convert to base64

    Args:
        img_url: URL of the image

    Returns:
        Base64 encoded image string or None if failed
    """
    try:
        response = requests.get(
            img_url,
            timeout=Config.REQUEST_TIMEOUT,
            stream=True
        )
        response.raise_for_status()

        # Check content type
        content_type = response.headers.get('content-type', '').lower()
        if not any(allowed in content_type for allowed in Config.ALLOWED_IMAGE_TYPES):
            logger.warning(f"Unsupported image type: {content_type} for {img_url}")
            return None

        # Check file size
        content_length = response.headers.get('content-length')
        if content_length and int(content_length) > Config.MAX_IMAGE_SIZE:
            logger.warning(f"Image too large: {img_url}")
            return None

        # Read content
        image_data = response.content
        if len(image_data) > Config.MAX_IMAGE_SIZE:
            logger.warning(f"Image too large after download: {img_url}")
            return None

        # Encode to base64
        img_base64 = base64.b64encode(image_data).decode('utf-8')
        return f'data:{content_type};base64,{img_base64}'

    except requests.RequestException as e:
        logger.error(f"Error fetching image {img_url}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error processing image {img_url}: {e}")
        return None


class Cleaner:
    """Clean and process HTML content from Sanfoundry pages"""

    def __init__(self):
        self.has_mathjax = False
        self.tags_to_remove = ['script', 'a', 'noscript', 'iframe', 'form']
        self.exclude_from_cleaning = ['br', 'img']

        self.ids_to_remove = [
            'sf-video-ads',
            'comments',
            'respond',
        ]

        self.classes_to_remove = [
            'mobile-content',
            'desktop-content',
            'sf-nav-bottom',
            'sf-desktop-ads',
            'sf-mobile-ads',
            'sf-video-yt',
            'sf-post-footer',
            'sf-post-content-category',
            'collapseomatic',
            'advertisement',
            'social-share',
        ]

        self.text_patterns_to_remove = (
            'Sanfoundry Global Education',
            'To practice',
            'Participate in',
            'to get free Certificate',
            'advertisement',
            'Next steps',
            'Manish Bhojasia',
        )

    def _should_remove_text(self, text: str) -> bool:
        """Check if text should be removed based on patterns"""
        return text and any(text.strip().startswith(pattern) for pattern in self.text_patterns_to_remove)

    def clean(self, soup: bs, page=None) -> Tuple[str, bool]:
        """
        Clean HTML content for PDF conversion

        Args:
            soup: BeautifulSoup object of the page
            page: Optional browser page instance for in-browser image fetching

        Returns:
            Tuple of (cleaned HTML string, has_mathjax flag)
        """
        try:
            # Check for MathJax
            self.has_mathjax = bool(soup.select('script[src*="mathjax"]'))

            # Find main content
            content = soup.find("div", {"class": "entry-content"})
            if not content:
                logger.warning("Could not find main content div")
                content = soup.find("article") or soup.find("main") or soup.body

            if not content:
                raise ValueError("No content found to clean")

            # Remove attributes of root tag
            content.attrs = {}

            # Process images
            self._process_images(content, page)

            # Remove unwanted elements
            self._remove_unwanted_elements(content)

            # Remove empty tags
            self._remove_empty_tags(content)

            # Clean text
            self._clean_text(content)

            return add_tags(content, self.has_mathjax)

        except Exception as e:
            logger.error(f"Error cleaning content: {e}", exc_info=True)
            raise

    def _process_images(self, content: bs, page=None) -> None:
        """Process and embed images as base64"""
        images = content.find_all('img')

        if not images:
            return

        logger.info(f"Processing {len(images)} images")

        # Prepare image URLs
        image_tasks = []
        for img in images:
            # Handle lazy loaded images
            if 'lazyload' in img.get('class', []):
                img.decompose()
                continue

            # Handle noscript wrapped images
            if img.parent and img.parent.name == 'noscript':
                try:
                    img.parent.unwrap()
                except ValueError:
                    pass

            # Get image source
            img_src = img.attrs.get('data-src') or img.attrs.get('src', '') if img.attrs else ''
            if img_src and img_src.startswith('http'):
                image_tasks.append((img, img_src))

        if page:
            # Fetch images inside the browser to bypass Cloudflare
            js_code = """
            return (async () => {
                try {
                    const response = await fetch(arguments[0]);
                    const blob = await response.blob();
                    return new Promise((resolve, reject) => {
                        const reader = new FileReader();
                        reader.onloadend = () => resolve(reader.result);
                        reader.onerror = reject;
                        reader.readAsDataURL(blob);
                    });
                } catch (e) {
                    return null;
                }
            })()
            """
            for img, url in image_tasks:
                try:
                    base64_data = page.run_js(js_code, url)
                    if base64_data and base64_data.startswith('data:image'):
                        img['src'] = base64_data
                        img.attrs = {'src': img['src']}
                    else:
                        img.decompose()
                except Exception as e:
                    logger.error(f"Error processing image {url} via browser JS: {e}")
                    img.decompose()
        else:
            # Fetch images in parallel
            with ThreadPoolExecutor(max_workers=Config.MAX_IMAGE_WORKERS) as executor:
                future_to_img = {
                    executor.submit(fetch_image_base64, url): (img, url)
                    for img, url in image_tasks
                }

                for future in as_completed(future_to_img):
                    img, url = future_to_img[future]
                    try:
                        base64_data = future.result()
                        if base64_data:
                            img['src'] = base64_data
                            # Remove other attributes
                            img.attrs = {'src': img['src']}
                        else:
                            # Remove failed images
                            img.decompose()
                    except Exception as e:
                        logger.error(f"Error processing image {url}: {e}")
                        img.decompose()

    def _remove_unwanted_elements(self, content: bs) -> None:
        """Remove unwanted HTML elements"""
        for tag in content.find_all():
            # Skip if tag is None or has been decomposed
            if tag is None or not tag.name:
                continue

            if tag.name in self.exclude_from_cleaning:
                continue

            # Remove specific tags
            if tag.name in self.tags_to_remove or tag.find("a", recursive=False):
                tag.decompose()
                continue

            # Remove by ID - check if attrs exists and is not None
            if tag.attrs and tag.attrs.get('id') in self.ids_to_remove:
                tag.decompose()
                continue

            # Remove by class - check if attrs exists and is not None
            if tag.attrs:
                tag_classes = tag.attrs.get('class', [])
                if any(cls in self.classes_to_remove for cls in tag_classes):
                    tag.decompose()
                    continue

            # Remove by text content
            if tag.string and self._should_remove_text(tag.get_text(strip=True)):
                tag.decompose()
                continue

            # Remove all attributes except for specific tags
            if tag.name not in ['img', 'table', 'tr', 'td', 'th']:
                tag.attrs = {}

    def _remove_empty_tags(self, content: bs) -> None:
        """Remove empty tags"""
        for tag in content.find_all():
            # Skip if tag is None or has been decomposed
            if tag is None or not tag.name:
                continue

            if tag.name in self.exclude_from_cleaning:
                continue
            if not tag.get_text(strip=True) and not tag.find('img'):
                tag.decompose()

    def _clean_text(self, content: bs) -> None:
        """Clean text content"""
        all_text = content.find_all(string=True)
        for text in all_text:
            if self._should_remove_text(text.strip()):
                text.replace_with('')
