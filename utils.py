import aiohttp
import aioboto3
import boto3
import io
import json
import logging
import os
from dotenv import load_dotenv
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from typing import Dict, Optional, List
import streamlit as st
from supabase_client import get_company_names, get_isin_by_name, get_quartrid_by_name, get_all_companies
import base64
import uuid

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Get environment variables
try:
    # Access secrets using flat structure (matching Streamlit Cloud)
    AWS_ACCESS_KEY_ID = st.secrets["AWS_ACCESS_KEY_ID"]
    AWS_SECRET_ACCESS_KEY = st.secrets["AWS_SECRET_ACCESS_KEY"]
    AWS_DEFAULT_REGION = st.secrets["AWS_DEFAULT_REGION"]
    S3_BUCKET_NAME = st.secrets["S3_BUCKET_NAME"]
    QUARTR_API_KEY = st.secrets["QUARTR_API_KEY"]
except KeyError:
    # Log the error and set default values
    logger.error("Failed to access secrets with flat structure, using fallback values")
    AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
    AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    AWS_DEFAULT_REGION = os.getenv("AWS_DEFAULT_REGION", "eu-central-2")
    S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "alpineinsights")
    QUARTR_API_KEY = os.getenv("QUARTR_API_KEY", "")

# Initialize Supabase client for storage
@st.cache_resource
def init_supabase_storage_client():
    """Initialize and cache the Supabase client for storage operations"""
    from supabase_client import init_client
    return init_client()

class SupabaseStorageHandler:
    """Handler for Supabase storage operations"""
    
    def __init__(self):
        self.client = init_supabase_storage_client()
        self.bucket_name = "alpinedatalake"
        
        # Check if bucket exists and create it if it doesn't
        try:
            self._ensure_bucket_exists()
        except Exception as e:
            logger.error(f"Error initializing Supabase storage bucket: {str(e)}")
    
    def _ensure_bucket_exists(self):
        """Ensure the storage bucket exists"""
        if self.client:
            try:
                # Check if bucket exists by listing buckets
                buckets = self.client.storage.list_buckets()
                bucket_names = [bucket["name"] for bucket in buckets]
                
                if self.bucket_name not in bucket_names:
                    logger.info(f"Creating Supabase storage bucket: {self.bucket_name}")
                    self.client.storage.create_bucket(self.bucket_name, {"public": True})
                    logger.info(f"Successfully created Supabase storage bucket: {self.bucket_name}")
            except Exception as e:
                logger.error(f"Error checking/creating Supabase bucket: {str(e)}")
    
    def create_filename(self, company_name: str, event_date: str, event_title: str, 
                       doc_type: str, original_filename: str) -> str:
        """Create a standardized filename with company, date, and type"""
        # Sanitize inputs to be safe for filenames
        safe_company = company_name.lower().replace(' ', '_').replace('-', '_')
        safe_date = event_date.replace('-', '')
        safe_title = event_title.lower().replace(' ', '_')[:30]  # Truncate to avoid very long filenames
        
        # Get file extension from original filename
        _, ext = os.path.splitext(original_filename)
        if not ext:
            ext = '.pdf'  # Default extension if none is found
        
        # Create path format: company/type/company_date_type.ext
        filename = f"{safe_company}/{doc_type}/{safe_company}_{safe_date}_{doc_type}{ext}"
        return filename
    
    async def upload_file(self, file_data: bytes, filename: str, content_type: str = 'application/pdf') -> bool:
        """Upload a file to Supabase storage
        
        Args:
            file_data: The binary file data
            filename: The target filename including path
            content_type: The MIME type of the file
        
        Returns:
            bool: True if upload was successful
        """
        if not self.client:
            logger.error("Supabase client not initialized")
            return False
            
        try:
            # Upload file to Supabase storage
            response = self.client.storage.from_(self.bucket_name).upload(
                path=filename,
                file=file_data,
                file_options={"content-type": content_type}
            )
            
            logger.info(f"Successfully uploaded {filename} to Supabase bucket {self.bucket_name}")
            return True
        except Exception as e:
            logger.error(f"Error uploading file to Supabase storage: {str(e)}")
            return False
    
    def get_public_url(self, filename: str) -> str:
        """Get the public URL for a file in Supabase storage"""
        if not self.client:
            return ""
            
        try:
            url = self.client.storage.from_(self.bucket_name).get_public_url(filename)
            return url
        except Exception as e:
            logger.error(f"Error getting public URL: {str(e)}")
            return ""
    
    async def download_file(self, filename: str, local_path: str) -> bool:
        """Download a file from Supabase storage to a local path
        
        Args:
            filename: The source filename in Supabase storage
            local_path: The local path to save the file
        
        Returns:
            bool: True if download was successful
        """
        if not self.client:
            logger.error("Supabase client not initialized")
            return False
            
        try:
            # Create the directory if it doesn't exist
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            
            # Download the file from Supabase storage
            response = self.client.storage.from_(self.bucket_name).download(filename)
            
            # Write the file to disk
            with open(local_path, 'wb') as f:
                f.write(response)
            
            logger.info(f"Successfully downloaded {filename} to {local_path}")
            return True
        except Exception as e:
            logger.error(f"Error downloading file from Supabase storage: {str(e)}")
            return False

class QuartrAPI:
    def __init__(self):
        if not QUARTR_API_KEY:
            raise ValueError("Quartr API key not found in environment variables")
        self.api_key = QUARTR_API_KEY
        self.base_url = "https://api.quartr.com/public/v1"
        self.headers = {"X-Api-Key": self.api_key}

    async def get_company_events(self, company_id: str, session: aiohttp.ClientSession, event_type: str = "all") -> Dict:
        """Get company events from Quartr API using company ID (not ISIN)"""
        url = f"{self.base_url}/companies/{company_id}/earlier-events"
        
        # Add query parameters
        params = {}
        if event_type != "all":
            params["type"] = event_type
        
        # Set limit to 10 to get enough events to select from
        params["limit"] = 10
        params["page"] = 1
        
        try:
            logger.info(f"Requesting earlier events from Quartr API for company ID: {company_id}")
            
            async with session.get(url, headers=self.headers, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.info(f"Successfully retrieved earlier events for company ID: {company_id}")
                    
                    events = data.get('data', [])
                    
                    # Return the events data only
                    return {
                        'events': events
                    }
                else:
                    response_text = await response.text()
                    logger.error(f"Error fetching earlier events for company ID {company_id}: Status {response.status}, Response: {response_text}")
                    return {}
        except Exception as e:
            logger.error(f"Exception while fetching earlier events for company ID {company_id}: {str(e)}")
            return {}

    async def _get_company_name_direct(self, company_id: str, session: aiohttp.ClientSession) -> str:
        """Direct method to get company name only"""
        try:
            url = f"{self.base_url}/companies/{company_id}"
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get('displayName', f"Company-{company_id}")
                return f"Company-{company_id}"
        except Exception:
            return f"Company-{company_id}"
    
    async def get_company_info(self, company_id: str, session: aiohttp.ClientSession) -> Dict:
        """Get basic company information using company ID"""
        url = f"{self.base_url}/companies/{company_id}"
        try:
            logger.info(f"Requesting company info from Quartr API for company ID: {company_id}")
            async with session.get(url, headers=self.headers) as response:
                if response.status == 200:
                    data = await response.json()
                    logger.info(f"Successfully retrieved company info for company ID: {company_id}")
                    return data
                else:
                    response_text = await response.text()
                    logger.error(f"Error fetching company info for company ID {company_id}: Status {response.status}, Response: {response_text}")
                    return {}
        except Exception as e:
            logger.error(f"Exception while fetching company info for company ID {company_id}: {str(e)}")
            return {}
    
    async def get_document(self, doc_url: str, session: aiohttp.ClientSession):
        """Get document from URL"""
        try:
            async with session.get(doc_url) as response:
                if response.status == 200:
                    return await response.read()
                else:
                    logger.error(f"Failed to fetch document from {doc_url}: {response.status}")
                    return None
        except Exception as e:
            logger.error(f"Error getting document from {doc_url}: {str(e)}")
            return None

class TranscriptProcessor:
    @staticmethod
    async def process_transcript(transcript_url: str, transcripts: Dict, session: aiohttp.ClientSession) -> str:
        """Process transcript JSON into clean text"""
        try:
            # First try to get the raw transcript URL from the transcripts object
            raw_transcript_url = None
            
            # Check for different transcript types in Quartr
            if 'transcriptUrl' in transcripts and transcripts['transcriptUrl']:
                raw_transcript_url = transcripts['transcriptUrl']
            elif 'finishedLiveTranscriptUrl' in transcripts.get('liveTranscripts', {}) and transcripts['liveTranscripts']['finishedLiveTranscriptUrl']:
                raw_transcript_url = transcripts['liveTranscripts']['finishedLiveTranscriptUrl']
            
            # If no raw transcript URL is found, try the app transcript URL
            if not raw_transcript_url and transcript_url and 'app.quartr.com' in transcript_url:
                # Convert app URL to API URL if possible
                document_id = transcript_url.split('/')[-2]
                if document_id.isdigit():
                    raw_transcript_url = f"https://api.quartr.com/public/v1/transcripts/document/{document_id}"
                    headers = {"X-Api-Key": QUARTR_API_KEY}
                    async with session.get(raw_transcript_url, headers=headers) as response:
                        if response.status == 200:
                            transcript_data = await response.json()
                            if transcript_data and 'transcript' in transcript_data:
                                text = transcript_data['transcript'].get('text', '')
                                if text:
                                    # Format the text with proper line breaks and cleanup
                                    formatted_text = TranscriptProcessor.format_transcript_text(text)
                                    logger.info(f"Successfully processed transcript from API, length: {len(formatted_text)}")
                                    return formatted_text
            
            # If we have a raw transcript URL, fetch and process it
            if raw_transcript_url:
                logger.info(f"Fetching transcript from: {raw_transcript_url}")
                
                try:
                    headers = {"X-Api-Key": QUARTR_API_KEY} if 'api.quartr.com' in raw_transcript_url else {}
                    async with session.get(raw_transcript_url, headers=headers) as response:
                        if response.status == 200:
                            # Try processing as JSON first
                            try:
                                transcript_data = await response.json()
                                # Handle different JSON formats
                                if 'transcript' in transcript_data:
                                    text = transcript_data['transcript'].get('text', '')
                                    if text:
                                        formatted_text = TranscriptProcessor.format_transcript_text(text)
                                        logger.info(f"Successfully processed JSON transcript, length: {len(formatted_text)}")
                                        return formatted_text
                                elif 'text' in transcript_data:
                                    formatted_text = TranscriptProcessor.format_transcript_text(transcript_data['text'])
                                    logger.info(f"Successfully processed simple JSON transcript, length: {len(formatted_text)}")
                                    return formatted_text
                            except json.JSONDecodeError:
                                # Not a JSON, try processing as text
                                text = await response.text()
                                if text:
                                    formatted_text = TranscriptProcessor.format_transcript_text(text)
                                    logger.info(f"Successfully processed text transcript, length: {len(formatted_text)}")
                                    return formatted_text
                        else:
                            logger.error(f"Failed to fetch transcript: {response.status}")
                except Exception as e:
                    logger.error(f"Error processing raw transcript: {str(e)}")
            
            logger.warning(f"No transcript found or could be processed for URL: {transcript_url}")
            return ''
        except Exception as e:
            logger.error(f"Error processing transcript: {str(e)}")
            return ''
    
    @staticmethod
    def format_transcript_text(text: str) -> str:
        """Format transcript text for better readability"""
        # Replace JSON line feed representations with actual line feeds
        text = text.replace('\\n', '\n')
        
        # Clean up extra whitespace
        text = ' '.join(text.split())
        
        # Format into paragraphs - break at sentence boundaries for better readability
        sentences = [s.strip() for s in text.split('.') if s.strip()]
        formatted_text = '.\n\n'.join(sentences) + '.'
        
        return formatted_text

    @staticmethod
    def create_pdf(company_name: str, event_title: str, event_date: str, transcript_text: str) -> bytes:
        """Create a PDF from transcript text"""
        if not transcript_text:
            logger.error("Cannot create PDF: Empty transcript text")
            return b''
            
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=letter,
            rightMargin=72,
            leftMargin=72,
            topMargin=72,
            bottomMargin=72
        )

        styles = getSampleStyleSheet()
        header_style = ParagraphStyle(
            'CustomHeader',
            parent=styles['Heading1'],
            fontSize=14,
            spaceAfter=30,
            textColor=colors.HexColor('#1a472a'),
            alignment=1
        )
        
        text_style = ParagraphStyle(
            'CustomText',
            parent=styles['Normal'],
            fontSize=10,
            leading=14,
            spaceBefore=6,
            fontName='Helvetica'
        )

        story = []
        
        # Create header with proper XML escaping
        header_text = f"""
            <para alignment="center">
            <b>{company_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')}</b><br/>
            <br/>
            Event: {event_title.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')}<br/>
            Date: {event_date}
            </para>
        """
        story.append(Paragraph(header_text, header_style))
        story.append(Spacer(1, 30))

        # Process transcript text
        paragraphs = transcript_text.split('\n\n')
        for para in paragraphs:
            if para.strip():
                # Clean and escape the text for PDF
                clean_para = para.strip().replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                try:
                    story.append(Paragraph(clean_para, text_style))
                    story.append(Spacer(1, 6))
                except Exception as e:
                    logger.error(f"Error adding paragraph to PDF: {str(e)}")
                    continue

        try:
            doc.build(story)
            pdf_data = buffer.getvalue()
            logger.info(f"Successfully created PDF, size: {len(pdf_data)} bytes")
            return pdf_data
        except Exception as e:
            logger.error(f"Error building PDF: {str(e)}")
            return b''

class S3Handler:
    def __init__(self):
        if not all([AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION]):
            raise ValueError("AWS credentials not found in environment variables")
        self.session = aioboto3.Session(
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_DEFAULT_REGION
        )
        self.boto3_session = boto3.Session(
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_DEFAULT_REGION
        )

    async def upload_file(self, file_data: bytes, filename: str, bucket_name: str, 
                         content_type: str = 'application/pdf') -> bool:
        """Upload file to S3 bucket"""
        try:
            async with self.session.client('s3') as s3:
                await s3.put_object(
                    Bucket=bucket_name,
                    Key=filename,
                    Body=file_data,
                    ContentType=content_type
                )
                logger.info(f"Successfully uploaded {filename} to S3 bucket {bucket_name}")
                return True
        except Exception as e:
            logger.error(f"Error uploading to S3: {str(e)}")
            return False

    def create_filename(self, company_name: str, event_date: str, event_title: str,
                       doc_type: str, original_filename: str) -> str:
        """Create standardized filename for S3"""
        clean_company = company_name.replace(" ", "_").replace("/", "_").lower()
        clean_event = event_title.replace(" ", "_").lower()
        clean_date = event_date.split("T")[0]
        
        # Remove any URL query parameters from the original filename
        if '?' in original_filename:
            original_filename = original_filename.split('?')[0]
        
        # Always use PDF extension for transcripts
        file_extension = "pdf" if doc_type == "transcript" else original_filename.split(".")[-1].lower()
        return f"{clean_company}_{clean_date}_{clean_event}_{doc_type}.{file_extension}"
    
    def create_presigned_url(self, bucket_name: str, key: str, expiration: int = 3600) -> Optional[str]:
        """Create a presigned URL for an S3 object"""
        try:
            s3_client = self.boto3_session.client('s3')
            response = s3_client.generate_presigned_url(
                'get_object',
                Params={'Bucket': bucket_name, 'Key': key},
                ExpiresIn=expiration
            )
            return response
        except Exception as e:
            logger.error(f"Error creating presigned URL: {str(e)}")
            return None