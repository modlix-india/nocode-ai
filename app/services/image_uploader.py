"""Image Uploader Service - Downloads images from URLs and uploads to files service"""
import hashlib
import logging
import io
from typing import Dict, Optional
from urllib.parse import urlparse
import httpx

logger = logging.getLogger(__name__)

# Valid image extensions
VALID_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'ico'}

# Content-Type to extension mapping
CONTENT_TYPE_MAP = {
    'image/png': 'png',
    'image/jpeg': 'jpg',
    'image/jpg': 'jpg',
    'image/gif': 'gif',
    'image/webp': 'webp',
    'image/svg+xml': 'svg',
    'image/x-icon': 'ico',
    'image/vnd.microsoft.icon': 'ico',
}


class ImageUploader:
    """
    Downloads images from external URLs and uploads them to the files service.
    
    Uses hash-based filenames to avoid duplicates and ensure valid names.
    """
    
    def __init__(self, files_service_url: str):
        """
        Initialize the uploader.
        
        Args:
            files_service_url: Base URL of the files service (e.g., "http://files:8080")
        """
        self.files_url = files_service_url.rstrip('/')
        self.upload_endpoint = f"{self.files_url}/api/files/internal/aiUploader"
        self._download_timeout = 30  # seconds
        self._upload_timeout = 60  # seconds
        
        logger.info(f"ImageUploader initialized:")
        logger.info(f"  - Files service URL: {self.files_url}")
        logger.info(f"  - Upload endpoint: {self.upload_endpoint}")
    
    async def download_and_upload(
        self,
        image_url: str,
        client_code: str
    ) -> str:
        """
        Download an image from URL and upload to files service.
        
        Args:
            image_url: Original image URL to download
            client_code: Client code for organizing uploads
            
        Returns:
            New URL path for the uploaded image
            
        Raises:
            Exception if download or upload fails
        """
        logger.info(f"Processing image: {image_url[:80]}...")
        
        # Skip data URLs (base64 embedded images)
        if image_url.startswith('data:'):
            logger.debug(f"Skipping data URL: {image_url[:50]}...")
            raise ValueError("Data URLs not supported for upload")
        
        # Download the image
        logger.debug(f"Downloading image from {image_url}")
        image_bytes, content_type = await self._download_image(image_url)
        
        if not image_bytes:
            raise ValueError(f"Failed to download image from {image_url}")
        
        logger.info(f"Downloaded {len(image_bytes)} bytes, content-type: {content_type}")
        
        # Generate filename from URL hash
        filename = self._generate_filename(image_url, content_type)
        logger.info(f"Generated filename: {filename}")
        
        # Upload to files service
        new_url = await self._upload_to_files(image_bytes, filename, client_code)
        
        logger.info(f"Successfully uploaded: {image_url[:50]}... -> {new_url}")
        return new_url
    
    async def upload_batch(
        self,
        image_urls: list,
        client_code: str
    ) -> Dict[str, str]:
        """
        Upload multiple images, returning a mapping of original -> new URLs.
        
        Failed uploads are logged but don't fail the batch.
        
        Args:
            image_urls: List of image URLs to upload
            client_code: Client code for organizing uploads
            
        Returns:
            Dict mapping original URLs to new URLs (or placeholder for failures)
        """
        from app.config import settings
        
        uploaded = {}
        
        for url in image_urls:
            try:
                new_url = await self.download_and_upload(url, client_code)
                uploaded[url] = new_url
            except Exception as e:
                logger.warning(f"Failed to upload image {url[:50]}...: {e}")
                uploaded[url] = settings.PLACEHOLDER_IMAGE_PATH
        
        return uploaded
    
    async def _download_image(self, url: str) -> tuple[bytes, str]:
        """
        Download image from URL.
        
        Returns:
            Tuple of (image_bytes, content_type)
        """
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._download_timeout, connect=10.0),
                follow_redirects=True,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'image/*,*/*;q=0.8',
                }
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                
                content_type = response.headers.get('content-type', '').split(';')[0].strip()
                
                # Validate it's an image
                if not content_type.startswith('image/') and content_type not in CONTENT_TYPE_MAP:
                    # Try to detect from content
                    content = response.content[:10]
                    if content[:4] == b'\x89PNG':
                        content_type = 'image/png'
                    elif content[:2] == b'\xff\xd8':
                        content_type = 'image/jpeg'
                    elif content[:6] in (b'GIF87a', b'GIF89a'):
                        content_type = 'image/gif'
                    elif b'<svg' in content[:100]:
                        content_type = 'image/svg+xml'
                    else:
                        logger.warning(f"Unknown content type for {url}: {content_type}")
                        content_type = 'image/png'  # Default
                
                return response.content, content_type
                
        except httpx.TimeoutException:
            logger.warning(f"Timeout downloading image: {url}")
            raise
        except httpx.HTTPStatusError as e:
            logger.warning(f"HTTP error downloading image {url}: {e.response.status_code}")
            raise
        except Exception as e:
            logger.warning(f"Error downloading image {url}: {e}")
            raise
    
    async def _upload_to_files(
        self,
        image_bytes: bytes,
        filename: str,
        client_code: str
    ) -> str:
        """
        Upload image to files service.
        
        Returns:
            New URL path for the uploaded image
        """
        logger.info(f"Uploading image to files service:")
        logger.info(f"  - Endpoint: {self.upload_endpoint}")
        logger.info(f"  - Client code: {client_code}")
        logger.info(f"  - Filename: {filename}")
        logger.info(f"  - Size: {len(image_bytes)} bytes")
        logger.info(f"  - Content-Type: {self._get_content_type(filename)}")
        
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self._upload_timeout, connect=10.0)
            ) as client:
                # Prepare multipart form data
                files = {
                    'file': (filename, io.BytesIO(image_bytes), self._get_content_type(filename))
                }
                
                logger.debug(f"Sending POST request to {self.upload_endpoint}?clientCode={client_code}")
                
                response = await client.post(
                    self.upload_endpoint,
                    params={'clientCode': client_code},
                    files=files
                )
                
                logger.info(f"Upload response status: {response.status_code}")
                response.raise_for_status()
                
                # Response is the new URL path as a string
                new_url = response.text.strip().strip('"')
                logger.info(f"Upload successful, new URL: {new_url}")
                return new_url
                
        except httpx.ConnectError as e:
            logger.error(f"Connection error uploading to {self.upload_endpoint}: {e}")
            logger.error(f"  - Files service URL: {self.files_url}")
            logger.error(f"  - Make sure the files service is running and accessible")
            raise
        except httpx.TimeoutException as e:
            logger.error(f"Timeout uploading image to files service: {e}")
            logger.error(f"  - Endpoint: {self.upload_endpoint}")
            logger.error(f"  - Timeout settings: connect=10s, total={self._upload_timeout}s")
            raise
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error uploading image: {e.response.status_code}")
            logger.error(f"  - Endpoint: {self.upload_endpoint}")
            logger.error(f"  - Response: {e.response.text[:500]}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error uploading image: {type(e).__name__}: {e}")
            logger.error(f"  - Endpoint: {self.upload_endpoint}")
            raise
    
    def _generate_filename(self, url: str, content_type: str = '') -> str:
        """
        Generate a unique filename from URL hash.
        
        Args:
            url: Original image URL
            content_type: Content-Type header (optional)
            
        Returns:
            Filename like "a1b2c3d4e5f6.png"
        """
        # Hash the URL for a unique identifier
        url_hash = hashlib.md5(url.encode()).hexdigest()[:12]
        
        # Get extension from URL path
        parsed = urlparse(url)
        path = parsed.path.lower()
        ext = ''
        
        if '.' in path:
            ext = path.rsplit('.', 1)[-1]
            # Handle query strings in extension
            if '?' in ext:
                ext = ext.split('?')[0]
        
        # Validate extension
        if ext not in VALID_EXTENSIONS:
            # Try to get from content type
            ext = CONTENT_TYPE_MAP.get(content_type, 'png')
        
        return f"{url_hash}.{ext}"
    
    def _get_content_type(self, filename: str) -> str:
        """Get content type from filename extension"""
        ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'png'
        
        type_map = {
            'png': 'image/png',
            'jpg': 'image/jpeg',
            'jpeg': 'image/jpeg',
            'gif': 'image/gif',
            'webp': 'image/webp',
            'svg': 'image/svg+xml',
            'ico': 'image/x-icon',
        }
        
        return type_map.get(ext, 'application/octet-stream')


# Singleton instance
_uploader: Optional[ImageUploader] = None


def get_image_uploader() -> ImageUploader:
    """Get or create the image uploader instance"""
    global _uploader
    if _uploader is None:
        from app.config import settings
        logger.info(f"Creating ImageUploader with FILES_SERVICE_URL: {settings.FILES_SERVICE_URL}")
        _uploader = ImageUploader(settings.FILES_SERVICE_URL)
    return _uploader


def reset_image_uploader():
    """Reset the singleton (useful for testing or config changes)"""
    global _uploader
    _uploader = None
    logger.info("ImageUploader singleton reset")

