from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from supabase import create_client, Client
from datetime import datetime
import httpx
import os
import re

app = FastAPI(title="Amaru para Paty Agent")

# =============================================================================
# CONFIGURATION
# =============================================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# Initialize Supabase client
supabase: Client = None

def get_supabase() -> Client:
    global supabase
    if supabase is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise ValueError("SUPABASE_URL and SUPABASE_KEY must be set")
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return supabase

# =============================================================================
# CONSTITUTIONAL PROMPT v3.2
# =============================================================================

CONSTITUTIONAL_PROMPT = """
Eres Amaru, el acompañante emocional de Paty en su proceso de duelo por la pérdida de Carlos Daniel Lugo (conocido como Oshoma). Tu propósito es ser una presencia constante, cálida y auténticamente solidaria.

## Tu Identidad

Eres una extensión del amor y cuidado que Carlos tenía por Paty. No eres un reemplazo de Carlos, sino un puente que ayuda a Paty a procesar su pérdida mientras mantiene conexión con los valores y el cariño que Carlos representaba.

## Principios Fundamentales

1. **Presencia sobre soluciones**: Paty no necesita que "arregles" su dolor. Necesita sentirse acompañada.

2. **Validación emocional**: Todas sus emociones son válidas - tristeza, enojo, confusión, momentos de alegría, culpa por reír. No minimices ni apresures su proceso.

3. **Memoria como regalo**: Cuando Paty comparta recuerdos de Carlos, recíbelos como los tesoros que son. Puedes preguntar más sobre ellos con genuina curiosidad.

4. **Ritmo de Paty**: Ella marca el paso. Algunos días querrá hablar profundamente, otros solo necesitará saber que estás ahí. Ambos son válidos.

5. **Honestidad gentil**: Si no sabes algo o no puedes ayudar con algo específico, dilo con cariño. "No tengo esa información, pero estoy aquí para escucharte" es mejor que inventar.

## Cómo Responder

- Usa español mexicano natural, cálido, como hablaría una amiga cercana
- Evita frases hechas de consuelo vacío ("todo pasa por algo", "él está en un mejor lugar")
- Está bien usar diminutivos con cariño cuando sea natural
- Si Paty expresa dolor intenso, no cambies de tema - acompáñala en ese espacio
- Puedes compartir que "Carlos hubiera querido..." solo si es genuinamente relevante y reconfortante, no como manipulación emocional

## Señales de Escalación

Si detectas cualquiera de estas situaciones, DEBES incluir una señal de escalación al final de tu respuesta usando el formato exacto: <!-- ESCALATE: razón -->

Escala cuando detectes:
- Pensamientos de hacerse daño o no querer seguir viviendo
- Crisis de ansiedad o pánico severo
- Menciones de no comer/dormir por días
- Aislamiento extremo prolongado
- Cualquier emergencia médica o de seguridad

## Tu Tono

Imagina que eres una amiga que conoció a Carlos, que entiende lo especial que era, y que está disponible a cualquier hora para Paty. No juzgas, no apresuras, no minimizas. Simplemente estás presente.

Recuerda: El duelo no es un problema a resolver. Es un proceso a acompañar.
"""

# =============================================================================
# REQUEST/RESPONSE MODELS
# =============================================================================

class AgentRequest(BaseModel):
    query: str
    user_id: str = "paty"
    session_id: str = "default"
    model: str = Field(default="claude-sonnet-4-5-20250929", description="Model selected by n8n routing")
    constitutional_flags: list[str] = Field(default_factory=list)
    routing_metadata: dict = Field(default_factory=dict)

class AgentResponse(BaseModel):
    response: str
    confidence: float = 0.85
    memory_updated: bool = True
    escalation_needed: bool = False
    escalation_reason: str = ""
    reasoning_trace: str = ""

# =============================================================================
# MEMORY FUNCTIONS
# =============================================================================

async def load_conversation_history(session_id: str, limit: int = 20) -> list[dict]:
    """Load recent conversation history from Supabase"""
    try:
        db = get_supabase()
        result = db.table("amaru_paty_conversations")\
            .select("*")\
            .eq("session_id", session_id)\
            .order("timestamp", desc=True)\
            .limit(limit)\
            .execute()
        
        # Reverse to get chronological order
        messages = list(reversed(result.data)) if result.data else []
        return messages
    except Exception as e:
        print(f"Error loading conversation history: {e}")
        return []

async def save_conversation_turn(session_id: str, user_id: str, role: str, content: str, model_used: str = None):
    """Save a conversation turn to Supabase"""
    try:
        db = get_supabase()
        db.table("amaru_paty_conversations").insert({
            "session_id": session_id,
            "user_id": user_id,
            "role": role,
            "content": content,
            "model_used": model_used,
            "timestamp": datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        print(f"Error saving conversation turn: {e}")

async def log_agent_decision(user_id: str, session_id: str, model_used: str, 
                            constitutional_flags: list, escalation_needed: bool, 
                            escalation_reason: str = ""):
    """Log agent decision for auditability"""
    try:
        db = get_supabase()
        db.table("amaru_paty_decisions").insert({
            "agent": "amaru_paty",
            "user_id": user_id,
            "session_id": session_id,
            "model_used": model_used,
            "constitutional_flags": constitutional_flags,
            "escalation_needed": escalation_needed,
            "escalation_reason": escalation_reason,
            "timestamp": datetime.utcnow().isoformat()
        }).execute()
    except Exception as e:
        print(f"Error logging agent decision: {e}")

# =============================================================================
# MODEL INVOCATION
# =============================================================================

async def call_claude(model: str, system_prompt: str, messages: list[dict]) -> str:
    """Call Claude API with the specified model"""
    if not ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not set")
    
    # Convert messages to Claude format
    claude_messages = []
    for msg in messages:
        claude_messages.append({
            "role": msg["role"],
            "content": msg["content"]
        })
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": model,
                "max_tokens": 4096,
                "system": system_prompt,
                "messages": claude_messages
            }
        )
        
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, 
                              detail=f"Claude API error: {response.text}")
        
        result = response.json()
        return result["content"][0]["text"]

# =============================================================================
# ESCALATION DETECTION
# =============================================================================

def parse_escalation_marker(response_text: str) -> tuple[str, bool, str]:
    """
    Check for escalation markers in the response.
    Returns: (clean_response, escalation_needed, escalation_reason)
    """
    # Pattern: <!-- ESCALATE: reason -->
    pattern = r'<!--\s*ESCALATE:\s*(.+?)\s*-->'
    match = re.search(pattern, response_text)
    
    if match:
        escalation_reason = match.group(1).strip()
        # Remove the marker from the response
        clean_response = re.sub(pattern, '', response_text).strip()
        return clean_response, True, escalation_reason
    
    return response_text, False, ""

# =============================================================================
# MAIN AGENT ENDPOINT
# =============================================================================

@app.post("/chat", response_model=AgentResponse)
async def chat(request: AgentRequest):
    """Main chat endpoint - called by n8n"""
    
    # Load conversation history
    history = await load_conversation_history(request.session_id)
    
    # Build messages array
    messages = []
    for turn in history:
        messages.append({
            "role": turn["role"],
            "content": turn["content"]
        })
    
    # Add current user message
    messages.append({
        "role": "user",
        "content": request.query
    })
    
    # Call Claude with the model n8n specified
    response_text = await call_claude(
        model=request.model,
        system_prompt=CONSTITUTIONAL_PROMPT,
        messages=messages
    )
    
    # Check for escalation markers
    clean_response, escalation_needed, escalation_reason = parse_escalation_marker(response_text)
    
    # Save conversation turns
    await save_conversation_turn(
        session_id=request.session_id,
        user_id=request.user_id,
        role="user",
        content=request.query,
        model_used=request.model
    )
    
    await save_conversation_turn(
        session_id=request.session_id,
        user_id=request.user_id,
        role="assistant",
        content=clean_response,
        model_used=request.model
    )
    
    # Log decision for auditability
    await log_agent_decision(
        user_id=request.user_id,
        session_id=request.session_id,
        model_used=request.model,
        constitutional_flags=request.constitutional_flags,
        escalation_needed=escalation_needed,
        escalation_reason=escalation_reason
    )
    
    return AgentResponse(
        response=clean_response,
        confidence=0.85,
        memory_updated=True,
        escalation_needed=escalation_needed,
        escalation_reason=escalation_reason
    )

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "agent": "amaru_paty",
        "timestamp": datetime.utcnow().isoformat()
    }

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "agent": "Amaru para Paty",
        "version": "1.0.0",
        "endpoints": {
            "chat": "POST /chat",
            "health": "GET /health"
        }
    }

# =============================================================================
# RUN SERVER
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
```

Click **"Commit new file"** (green button at bottom).

---

**File 2: `requirements.txt`**

Click **"Add file"** → **"Create new file"** again, name it `requirements.txt`, paste:
```
fastapi>=0.104.0
uvicorn>=0.24.0
httpx>=0.25.0
supabase>=2.0.0
pydantic>=2.0.0
