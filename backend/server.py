from fastapi import FastAPI, APIRouter, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional
import uuid
from datetime import datetime, timezone
from emergentintegrations.llm.chat import LlmChat, UserMessage


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Create the main app without a prefix
app = FastAPI()

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")


# Define Models
class ChatSession(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str = "New Chat"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Message(BaseModel):
    model_config = ConfigDict(extra="ignore")
    
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    chat_id: str
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ChatCreate(BaseModel):
    title: Optional[str] = "New Chat"


class MessageCreate(BaseModel):
    chat_id: str
    content: str


class MessageResponse(BaseModel):
    message: Message
    response: str


# Chat Session Routes
@api_router.post("/chats", response_model=ChatSession)
async def create_chat(input: ChatCreate):
    chat = ChatSession(title=input.title)
    doc = chat.model_dump()
    doc['created_at'] = doc['created_at'].isoformat()
    doc['updated_at'] = doc['updated_at'].isoformat()
    
    await db.chats.insert_one(doc)
    return chat


@api_router.get("/chats", response_model=List[ChatSession])
async def get_chats():
    chats = await db.chats.find({}, {"_id": 0}).sort("updated_at", -1).to_list(1000)
    
    for chat in chats:
        if isinstance(chat['created_at'], str):
            chat['created_at'] = datetime.fromisoformat(chat['created_at'])
        if isinstance(chat['updated_at'], str):
            chat['updated_at'] = datetime.fromisoformat(chat['updated_at'])
    
    return chats


@api_router.delete("/chats/{chat_id}")
async def delete_chat(chat_id: str):
    # Delete chat and all its messages
    await db.chats.delete_one({"id": chat_id})
    await db.messages.delete_many({"chat_id": chat_id})
    return {"status": "deleted"}


# Message Routes
@api_router.get("/chats/{chat_id}/messages", response_model=List[Message])
async def get_messages(chat_id: str):
    messages = await db.messages.find({"chat_id": chat_id}, {"_id": 0}).sort("timestamp", 1).to_list(1000)
    
    for msg in messages:
        if isinstance(msg['timestamp'], str):
            msg['timestamp'] = datetime.fromisoformat(msg['timestamp'])
    
    return messages


@api_router.post("/chats/{chat_id}/messages", response_model=MessageResponse)
async def send_message(chat_id: str, input: MessageCreate):
    # Verify chat exists
    chat = await db.chats.find_one({"id": chat_id}, {"_id": 0})
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    
    # Save user message
    user_message = Message(
        chat_id=chat_id,
        role="user",
        content=input.content
    )
    user_msg_doc = user_message.model_dump()
    user_msg_doc['timestamp'] = user_msg_doc['timestamp'].isoformat()
    await db.messages.insert_one(user_msg_doc)
    
    # Get chat history for context
    messages_history = await db.messages.find(
        {"chat_id": chat_id}, 
        {"_id": 0}
    ).sort("timestamp", 1).to_list(1000)
    
    # Prepare messages for LLM
    try:
        # Initialize LLM chat
        llm_chat = LlmChat(
            api_key=os.environ.get('EMERGENT_LLM_KEY'),
            session_id=chat_id,
            system_message="You are WormGPT, a highly capable AI assistant. When providing code, always format it properly with language-specific syntax."
        ).with_model("openai", "gpt-4o-mini")
        
        # Send message to LLM
        llm_user_message = UserMessage(text=input.content)
        ai_response = await llm_chat.send_message(llm_user_message)
        
        # Save AI response
        ai_message = Message(
            chat_id=chat_id,
            role="assistant",
            content=ai_response
        )
        ai_msg_doc = ai_message.model_dump()
        ai_msg_doc['timestamp'] = ai_msg_doc['timestamp'].isoformat()
        await db.messages.insert_one(ai_msg_doc)
        
        # Update chat's updated_at timestamp
        await db.chats.update_one(
            {"id": chat_id},
            {"$set": {"updated_at": datetime.now(timezone.utc).isoformat()}}
        )
        
        return MessageResponse(message=user_message, response=ai_response)
        
    except Exception as e:
        logging.error(f"Error calling LLM: {str(e)}")
        raise HTTPException(status_code=500, detail=f"AI Error: {str(e)}")


@api_router.get("/")
async def root():
    return {"message": "WormGPT API is running"}


# Include the router in the main app
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
