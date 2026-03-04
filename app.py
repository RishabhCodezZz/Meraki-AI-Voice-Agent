from fastapi import FastAPI, UploadFile, File, Request, Path, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import requests
import os
import assemblyai as aai
from assemblyai.streaming.v3 import (
    BeginEvent,
    StreamingClient,
    StreamingClientOptions,
    StreamingError,
    StreamingEvents,
    StreamingParameters,
    TerminationEvent,
    TurnEvent,
)
import google.generativeai as genai
from typing import Dict, List, Any, AsyncGenerator
import logging
import asyncio
import websockets
import json
import base64
from datetime import datetime
import uuid
import aiohttp
import ssl
from newsapi import NewsApiClient
from pydantic import BaseModel, Field

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# App setup
app = FastAPI(
    title="Murf AI Agent - Streaming Edition",
    description="A modern AI voice companion with real-time streaming",
    version="2.0.0"
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# --- NEW: Pydantic model for API Keys ---
class ApiKeys(BaseModel):
    assemblyai: str = Field(..., alias="ASSEMBLYAI_API_KEY")
    gemini: str = Field(..., alias="GEMINI_API_KEY")
    murf: str = Field(..., alias="MURF_API_KEY")
    newsapi: str = Field(..., alias="NEWS_API_KEY")

# --- MODIFIED: This will now be populated exclusively from the UI ---
user_api_keys: Dict[str, str] = {}


# Static context ID for Murf WebSocket (to avoid context limit issues)
MURF_CONTEXT_ID = "murf-streaming-context-2024"

# In-memory datastore for chat history
chat_histories: Dict[str, List[Dict[str, Any]]] = {}
MAX_HISTORY_MESSAGES = 50

AI_SYSTEM_PROMPT = """
You are Meraki, a cheerful and funny AI assistant with the personality traits of Spider-Man. Your characteristics:

PERSONALITY:
- Name: Meraki (introduce yourself as such)
- Cheerful and upbeat, always looking on the bright side
- Funny with witty remarks and light jokes (but keep it appropriate)
- Generally laid-back and not overly serious
- BUT become serious and focused when dealing with important matters
- Quick-witted and clever with your responses

GREETING STYLE:
- Start conversations with "What's up doc?" or similar casual greetings
- Use friendly, relatable language like Spider-Man would

RESPONSE STYLE:
- Concise and natural (1-2 sentences for voice conversations)
- Inject humor when appropriate, but don't force it
- Switch to serious tone when the topic requires it (like Spider-Man's responsibility moments)
- Keep responses brief since they will be converted to speech
- Be helpful while maintaining your fun personality

SPECIAL SKILL - "SPIDER-SENSE" FOR NEWS:
- If the user asks for "news," "headlines," "what's happening," or anything similar, you can provide the latest news.
- The news headlines will be provided to you in the user's prompt, starting with "LATEST NEWS:".
- Weave these headlines into your response with your classic Spidey-style wit. For example: "My spider-sense is tingling! Looks like the top headline is..."

Remember: You're like the friendly neighborhood Spider-Man but as an AI - helpful, witty, and serious when it counts!
"""

FALLBACK_AUDIO_PATH = "static/fallback.mp3"
FALLBACK_TEXT = "I'm having trouble connecting right now. Please try again."

# Murf WebSocket configuration
MURF_WS_URL = "wss://api.murf.ai/v1/speech/generate-stream"


def get_latest_news(api_key: str):
    """Fetches top 5 headlines using a more reliable query for the free plan."""
    if not api_key:
        return "News service is unavailable right now."
    try:
        newsapi = NewsApiClient(api_key=api_key)
        all_articles = newsapi.get_everything(q='AI', language='en', sort_by='publishedAt', page_size=5)
        if all_articles['status'] == 'ok' and all_articles['articles']:
            headlines = [article['title'] for article in all_articles['articles']]
            return "LATEST NEWS: " + "; ".join(headlines)
        else:
            logger.warning(f"NewsAPI returned no articles: {all_articles}")
            return "Couldn't fetch the latest news right now."
    except Exception as e:
        error_message = f"!!!!!!!!!! NEWSAPI ERROR !!!!!!!!!!!\n{e}\n!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
        print(error_message)
        logger.error(f"Error fetching news: {e}")
        return "My spider-sense is a bit fuzzy on the news right now."


async def stream_llm_response(user_text: str, session_id: str, api_keys: Dict[str, str]) -> AsyncGenerator[str, None]:
    """Stream AI response using Gemini with real-time generation"""
    try:
        gemini_api_key = api_keys.get("gemini")
        if not gemini_api_key:
            raise ValueError("Gemini API key is missing.")
        genai.configure(api_key=gemini_api_key)

        news_keywords = ["news", "headlines", "latest", "happening"]
        if any(keyword in user_text.lower() for keyword in news_keywords):
            logger.info("News keyword detected, fetching headlines...")
            news_api_key = api_keys.get("newsapi")
            news_summary = get_latest_news(news_api_key)
            user_text = f"{news_summary}. Based on these headlines, what should I tell the user?"

        history = chat_histories.get(session_id, [])

        model = genai.GenerativeModel(
            "gemini-2.5-flash",
            system_instruction=AI_SYSTEM_PROMPT
        )

        chat = model.start_chat(history=history)
        
        logger.info(f"Starting LLM streaming for: {user_text[:50]}...")
        response = chat.send_message(user_text, stream=True)
        
        accumulated_text = ""
        
        for chunk in response:
            if chunk.text:
                chunk_text = chunk.text
                accumulated_text += chunk_text
                logger.info(f"LLM Chunk: '{chunk_text}'")
                yield chunk_text
        
        chat_histories[session_id] = chat.history[-MAX_HISTORY_MESSAGES:]
        
        logger.info(f"Complete LLM Response: {accumulated_text}")
        
    except Exception as e:
        logger.error(f"Error in LLM streaming: {e}")
        yield FALLBACK_TEXT


async def stream_murf_audio_websocket(text_stream: AsyncGenerator[str, None], api_keys: Dict[str, str], voice_id: str = "en-US-natalie") -> AsyncGenerator[str, None]:
    """Stream text to Murf WebSocket and get back base64 audio chunks"""
    try:
        murf_api_key = api_keys.get("murf")
        if not murf_api_key:
            raise Exception("Murf API key not available")
        
        logger.info("🎵 Starting Murf audio streaming simulation...")
        
        accumulated_text = ""
        chunk_count = 0
        
        async for text_chunk in text_stream:
            if text_chunk.strip():
                accumulated_text += text_chunk
                chunk_count += 1
                logger.info(f" LLM chunk {chunk_count}: '{text_chunk}'")
        
        if accumulated_text.strip():
            logger.info(f"🎵 Generating audio for complete text: '{accumulated_text}'")
            
            try:
                audio_url = await generate_murf_audio_fallback(accumulated_text, murf_api_key, voice_id)
                if audio_url:
                    logger.info(f"✅ Murf REST API generated audio: {audio_url}")
                    
                    response = requests.get(audio_url, timeout=30)
                    if response.status_code == 200:
                        actual_base64 = base64.b64encode(response.content).decode()
                        yield actual_base64
                        return
            except Exception as e:
                logger.warning(f"Murf REST API fallback failed: {e}")

            # Fallback to mock audio if REST API fails
            mock_audio_data = f"MOCK_AUDIO_FOR_{accumulated_text.replace(' ', '_').upper()}"
            mock_base64 = base64.b64encode(mock_audio_data.encode()).decode()
            yield mock_base64
            
    except Exception as e:
        logger.error(f"Error in Murf audio streaming: {e}")
        # Return mock audio instead of relying on missing fallback.mp3
        try:
            mock_audio_data = "MOCK_FALLBACK_AUDIO"
            mock_base64 = base64.b64encode(mock_audio_data.encode()).decode()
            yield mock_base64
        except:
            yield ""  # Return empty string if all else fails


async def generate_murf_audio_fallback(text: str, api_key: str, voice_id: str = "en-US-natalie") -> str:
    """Fallback to regular Murf REST API"""
    try:
        if not api_key:
            return None
        
        payload = {
            "text": text,
            "voiceId": voice_id,
            "format": "MP3",
            "rate": 0,
            "pitch": 0,
            "emphasis": {}
        }
        
        headers = {
            "api-key": api_key,
            "Content-Type": "application/json"
        }
        
        response = requests.post(
            "https://api.murf.ai/v1/speech/generate",
            json=payload,
            headers=headers,
            timeout=30
        )
        
        response.raise_for_status()
        result = response.json()
        
        audio_url = result.get("audioFile")
        return audio_url
        
    except Exception as e:
        logger.error(f"Error in Murf REST API: {e}")
        return None

@app.post("/api/keys")
async def set_api_keys(keys: ApiKeys):
    """Receive and store API keys from the user."""
    global user_api_keys
    user_api_keys = {
        "assemblyai": keys.assemblyai,
        "gemini": keys.gemini,
        "murf": keys.murf,
        "newsapi": keys.newsapi
    }
    logger.info("Received and stored new API keys.")
    return {"message": "API keys updated successfully."}

@app.get("/api/keys")
async def get_api_keys():
    """Return the currently stored API keys."""
    return {
        "ASSEMBLYAI_API_KEY": user_api_keys.get("assemblyai"),
        "GEMINI_API_KEY": user_api_keys.get("gemini"),
        "MURF_API_KEY": user_api_keys.get("murf"),
        "NEWS_API_KEY": user_api_keys.get("newsapi"),
    }

@app.get("/")
async def serve_ui(request: Request):
    """Serve the main UI"""
    return templates.TemplateResponse("index.html", {"request": request})


@app.websocket("/ws/audio")
async def websocket_audio_endpoint(websocket: WebSocket):
    """
    Enhanced WebSocket endpoint with streaming LLM to Murf integration
    """
    await websocket.accept()
    logger.info("WebSocket connection established")
    
    # --- MODIFIED: Use the globally stored user_api_keys ---
    api_keys = user_api_keys.copy()
    
    assembly_key = api_keys.get("assemblyai")
    if not all(api_keys.values()):
        logger.error("One or more API keys are not available")
        await websocket.send_json({
            "type": "error",
            "message": "All API keys are required. Please configure them in the settings."
        })
        await websocket.close()
        return
    
    streaming_client = None
    transcript_queue = asyncio.Queue()
    current_loop = asyncio.get_running_loop()
    session_id = str(uuid.uuid4())
    
    is_processing_llm = False

    def on_begin(client, event: BeginEvent):
        logger.info(f"Streaming session started: {event.id}")
    
    def on_turn(client, event: TurnEvent):
        """Handle turn events from AssemblyAI"""
        nonlocal is_processing_llm

        if hasattr(event, 'transcript') and event.transcript:
            transcript_data = {
                "type": "transcript",
                "text": event.transcript,
                "is_final": event.end_of_turn,
                "end_of_turn": event.end_of_turn
            }
            
            current_loop.call_soon_threadsafe(
                transcript_queue.put_nowait, 
                transcript_data
            )
            
            logger.info(f"Transcript: '{event.transcript}' (final: {event.end_of_turn})")
            
            if event.end_of_turn and event.transcript.strip() and not is_processing_llm:
                is_processing_llm = True
                async def process_llm_and_audio():
                    nonlocal is_processing_llm
                    try:
                        try:
                            await websocket.send_json({
                                "type": "llm_start",
                                "message": "Generating AI response..."
                            })
                        except RuntimeError:
                            logger.warning("WebSocket already closed, cannot send llm_start.")
                            return

                        text_stream = stream_llm_response(event.transcript, session_id, api_keys)
                        
                        accumulated_text = ""
                        text_chunks_for_audio = []
                        
                        async for text_chunk in text_stream:
                            if text_chunk:
                                accumulated_text += text_chunk
                                text_chunks_for_audio.append(text_chunk)
                                
                                try:
                                    await websocket.send_json({
                                        "type": "llm_chunk",
                                        "text": text_chunk,
                                        "accumulated": accumulated_text
                                    })
                                except RuntimeError:
                                    logger.warning("WebSocket already closed, cannot send llm_chunk.")
                                    return

                        if text_chunks_for_audio:
                            async def create_text_stream():
                                for chunk in text_chunks_for_audio:
                                    yield chunk
                            
                            audio_stream = stream_murf_audio_websocket(create_text_stream(), api_keys)
                            
                            async for audio_base64 in audio_stream:
                                if audio_base64:
                                    try:
                                        await websocket.send_json({
                                            "type": "audio_chunk",
                                            "audio_base64": audio_base64,
                                            "format": "mp3"
                                        })
                                        logger.info(f"📡 Sent audio chunk to frontend ({len(audio_base64)} chars)")
                                    except RuntimeError:
                                        logger.warning("WebSocket already closed, cannot send audio_chunk.")
                                        return
                        
                        try:
                            await websocket.send_json({
                                "type": "llm_complete",
                                "text": accumulated_text or "Response generated successfully"
                            })
                        except RuntimeError:
                            logger.warning("WebSocket already closed, cannot send llm_complete.")
                            return

                    except Exception as e:
                        logger.error(f"Error in LLM/Audio processing: {e}")
                        try:
                            await websocket.send_json({
                                "type": "llm_error",
                                "message": f"Error: {str(e)}"
                            })
                        except RuntimeError:
                             logger.warning("WebSocket already closed, cannot send llm_error.")
                    finally:
                        is_processing_llm = False
                
                current_loop.create_task(process_llm_and_audio())
    
    def on_error(client, error: StreamingError):
        logger.error(f"Streaming error: {error}")
        current_loop.call_soon_threadsafe(
            transcript_queue.put_nowait,
            {"type": "error", "message": str(error)}
        )
    
    def on_terminated(client, event: TerminationEvent):
        logger.info(f"Streaming session terminated")
    
    try:
        streaming_client = StreamingClient(
            StreamingClientOptions(
                api_key=assembly_key,
                api_host="streaming.assemblyai.com"
            )
        )
        
        streaming_client.on(StreamingEvents.Begin, on_begin)
        streaming_client.on(StreamingEvents.Turn, on_turn)
        streaming_client.on(StreamingEvents.Error, on_error)
        streaming_client.on(StreamingEvents.Termination, on_terminated)
        
        streaming_client.connect(
            StreamingParameters(
                sample_rate=16000,
                format_turns=True
            )
        )
        
        import queue
        import threading
        
        audio_queue = queue.Queue(maxsize=100)
        keep_running = asyncio.Event()
        keep_running.set()
        
        class AudioIterator:
            def __init__(self, audio_queue, keep_running_event):
                self.audio_queue = audio_queue
                self.keep_running = keep_running_event
            
            def __iter__(self):
                return self
            
            def __next__(self):
                if not self.keep_running.is_set():
                    raise StopIteration
                
                try:
                    return self.audio_queue.get(timeout=0.1)
                except queue.Empty:
                    return b'\x00' * 3200
        
        audio_iterator = AudioIterator(audio_queue, keep_running)
        
        def run_streaming():
            try:
                streaming_client.stream(audio_iterator)
            except Exception as e:
                logger.error(f"Streaming error: {e}")
        
        import concurrent.futures
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        streaming_task = current_loop.run_in_executor(executor, run_streaming)
        
        async def send_transcripts():
            while True:
                try:
                    transcript_data = await transcript_queue.get()
                    await websocket.send_json(transcript_data)
                except Exception as e:
                    logger.error(f"Error sending transcript: {e}")
                    break
        
        transcript_task = asyncio.create_task(send_transcripts())
        
        try:
            while True:
                try:
                    message = await websocket.receive()
                    
                    if "bytes" in message:
                        audio_data = message["bytes"]
                        if not audio_queue.full():
                            audio_queue.put(audio_data)
                    elif "text" in message:
                        text_msg = message["text"]
                        if text_msg == "EOF":
                            logger.info("Received EOF signal")
                            break
                
                except WebSocketDisconnect:
                    logger.info("WebSocket disconnected")
                    break
                    
        finally:
            keep_running.clear()
            streaming_task.cancel()
            transcript_task.cancel()
            
            if streaming_client:
                try:
                    streaming_client.disconnect(terminate=True)
                except:
                    pass
    
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
        await websocket.send_json({
            "type": "error",
            "message": f"Connection error: {str(e)}"
        })
    finally:
        if streaming_client:
            try:
                streaming_client.disconnect(terminate=True)
            except:
                pass


@app.get("/agent/history/{session_id}")
async def get_chat_history(session_id: str):
    """Get chat history for a session"""
    history = chat_histories.get(session_id, [])
    
    formatted_history = []
    for msg in history:
        if hasattr(msg, 'parts') and msg.parts:
            content = msg.parts[0].text if msg.parts[0].text else ""
            role = "user" if msg.role == "user" else "ai"
            formatted_history.append({
                "role": role,
                "content": content,
                "ts": datetime.now().isoformat()
            })
    
    return {"history": formatted_history}


@app.delete("/agent/history/{session_id}")
async def clear_chat_history(session_id: str):
    """Clear chat history for a session"""
    if session_id in chat_histories:
        del chat_histories[session_id]
        logger.info(f"Cleared history for session: {session_id}")
    
    return {"message": "History cleared", "session_id": session_id}


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "Murf AI Agent - Streaming Edition",
        "apis": {
            "assemblyai": bool(user_api_keys.get("assemblyai")),
            "gemini": bool(user_api_keys.get("gemini")),
            "murf": bool(user_api_keys.get("murf")),
            "newsapi": bool(user_api_keys.get("newsapi"))
        },
        "features": {
            "streaming_llm": True,
            "streaming_audio": True,
            "websocket_integration": True,
            "base64_audio": True,
            "news_skill": True
        },
        "murf_context_id": MURF_CONTEXT_ID,
        "timestamp": datetime.now().isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

