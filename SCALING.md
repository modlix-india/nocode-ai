# Scaling the AI Service for 100+ Concurrent Users

## Current Bottlenecks

1. **Claude API Rate Limits** - Most critical
2. **Single-process FastAPI** - Limited concurrency
3. **Local ChromaDB** - Not suitable for multiple workers
4. **No request queuing** - Requests can overwhelm the system
5. **No caching** - Repeated similar queries hit Claude

---

## Phase 1: Quick Wins (Immediate)

### 1.1 Multi-Worker FastAPI with Gunicorn

```bash
# Run with multiple workers
gunicorn app.main:app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:5001 \
  --timeout 300 \
  --keep-alive 300
```

**Dockerfile update:**

```dockerfile
CMD ["gunicorn", "app.main:app", \
     "--workers", "4", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:5001", \
     "--timeout", "300"]
```

### 1.2 Add Request Rate Limiting

```python
# app/middleware/rate_limit.py
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

# In main.py
app.state.limiter = limiter

# In route
@router.post("/agent/page")
@limiter.limit("5/minute")  # 5 requests per minute per user
async def generate_page(...):
    ...
```

### 1.3 Implement Response Caching

```python
# For identical prompts on same page, return cached response
import hashlib
from functools import lru_cache

def get_cache_key(page_id: str, prompt: str) -> str:
    return hashlib.md5(f"{page_id}:{prompt}".encode()).hexdigest()
```

---

## Phase 2: Production Architecture (1-2 weeks)

### 2.1 Redis for Queuing & Caching

```yaml
# docker-compose.prod.yml
services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    volumes:
      - redis_data:/data
    command: redis-server --appendonly yes

  ai-service:
    build: .
    environment:
      - REDIS_URL=redis://redis:6379
    depends_on:
      - redis
```

### 2.2 Celery Workers for Claude API Calls

```python
# app/worker.py
from celery import Celery

celery_app = Celery(
    'ai_worker',
    broker='redis://redis:6379/0',
    backend='redis://redis:6379/1'
)

@celery_app.task(bind=True, max_retries=3, rate_limit='10/m')
def call_claude_agent(self, agent_name: str, input_data: dict):
    """
    Rate-limited Claude API call.
    10 per minute per worker = 30/min with 3 workers
    """
    try:
        # Agent execution logic
        pass
    except anthropic.RateLimitError as e:
        # Exponential backoff
        self.retry(countdown=2 ** self.request.retries * 10)
```

### 2.3 ChromaDB Server Mode

```yaml
# docker-compose.prod.yml
services:
  chromadb:
    image: chromadb/chroma:latest
    ports:
      - "8000:8000"
    volumes:
      - chroma_data:/chroma/chroma
    environment:
      - ANONYMIZED_TELEMETRY=False

  ai-service:
    environment:
      - CHROMA_HOST=chromadb
      - CHROMA_PORT=8000
```

```python
# app/rag/client.py
import chromadb

def get_chroma_client():
    if settings.CHROMA_HOST:
        return chromadb.HttpClient(
            host=settings.CHROMA_HOST,
            port=settings.CHROMA_PORT
        )
    return chromadb.PersistentClient(path=settings.CHROMA_PATH)
```

---

## Phase 3: Kubernetes Deployment (Production)

### 3.1 Kubernetes Manifests

```yaml
# k8s/deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ai-service
spec:
  replicas: 3
  selector:
    matchLabels:
      app: ai-service
  template:
    spec:
      containers:
        - name: ai-service
          image: your-registry/nocode-ai:latest
          resources:
            requests:
              memory: "512Mi"
              cpu: "500m"
            limits:
              memory: "2Gi"
              cpu: "2000m"
          env:
            - name: WORKERS
              value: "2"
          livenessProbe:
            httpGet:
              path: /health
              port: 5001
            initialDelaySeconds: 30
            periodSeconds: 10
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: ai-service-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: ai-service
  minReplicas: 3
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
```

### 3.2 Celery Workers Deployment

```yaml
# k8s/celery-worker.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ai-celery-worker
spec:
  replicas: 5
  template:
    spec:
      containers:
        - name: celery-worker
          image: your-registry/nocode-ai:latest
          command:
            [
              "celery",
              "-A",
              "app.worker",
              "worker",
              "--loglevel=info",
              "--concurrency=2",
            ]
          resources:
            requests:
              memory: "1Gi"
              cpu: "500m"
            limits:
              memory: "2Gi"
              cpu: "1000m"
```

---

## Capacity Planning

### Claude API Limits (Typical)

| Tier   | RPM   | TPM     |
| ------ | ----- | ------- |
| Free   | 50    | 40,000  |
| Tier 1 | 1,000 | 80,000  |
| Tier 2 | 2,000 | 160,000 |
| Tier 3 | 4,000 | 400,000 |

### Your Usage Per Request

| Agent                        | Tokens (approx) |
| ---------------------------- | --------------- |
| Layout Analyzer (Haiku)      | ~2,000          |
| Layout Generator (Sonnet)    | ~4,000          |
| Component Analyzer (Haiku)   | ~2,000          |
| Component Generator (Sonnet) | ~6,000          |
| Events Analyzer (Haiku)      | ~2,000          |
| Events Generator (Sonnet)    | ~6,000          |
| Styles (Haiku)               | ~3,000          |
| Animation (Haiku)            | ~2,000          |
| Review (Sonnet)              | ~5,000          |
| **Total per request**        | **~32,000**     |

### Capacity Calculation

```
With Tier 2 limits (160,000 TPM):
- Max concurrent generations: 160,000 / 32,000 = 5 per minute
- With 100 users at 10% concurrent usage: 10 requests/min needed
- **Need Tier 3 or higher for 100 users**
```

---

## Optimization Strategies

### 1. Request Deduplication

```python
# If same user submits while request in progress, return existing
active_requests = {}  # user_id -> task_id

async def generate_page(user_id: str, request: PageRequest):
    if user_id in active_requests:
        return {"status": "already_processing", "task_id": active_requests[user_id]}

    task_id = start_generation(request)
    active_requests[user_id] = task_id
    return {"task_id": task_id}
```

### 2. Smart Agent Selection

```python
# Skip agents that aren't needed
def get_required_agents(request: PageRequest) -> List[str]:
    agents = []

    if "layout" in request.instruction.lower():
        agents.append("layout")
    if "style" in request.instruction.lower() or "color" in request.instruction.lower():
        agents.append("styles")
    # ... etc

    # Always include review
    agents.append("review")
    return agents
```

### 3. Prompt Caching (Anthropic Feature)

```python
# Use Anthropic's prompt caching for system prompts
# This can reduce token usage by 90% for cached portions
response = client.messages.create(
    model="claude-sonnet-4-20250514",
    max_tokens=8192,
    system=[
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"}  # Cache this
        }
    ],
    messages=messages
)
```

### 4. Response Streaming to Reduce Perceived Latency

Already implemented with SSE - users see progress immediately.

---

## Quick Start for Dev Environment

```bash
# 1. Start Redis
docker run -d --name redis -p 6379:6379 redis:7-alpine

# 2. Start ChromaDB server
docker run -d --name chromadb -p 8000:8000 chromadb/chroma:latest

# 3. Update .env
REDIS_URL=redis://localhost:6379
CHROMA_HOST=localhost
CHROMA_PORT=8000

# 4. Start with multiple workers
gunicorn app.main:app \
  --workers 4 \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:5001 \
  --timeout 300

# 5. Start Celery workers (separate terminal)
celery -A app.worker worker --loglevel=info --concurrency=2
```

---

## Monitoring & Alerts

### Key Metrics to Track

1. **API Metrics**

   - Request latency (p50, p95, p99)
   - Error rate
   - Requests per second

2. **Claude API Metrics**

   - Tokens used per request
   - Rate limit hits
   - API errors

3. **Queue Metrics**
   - Queue depth
   - Processing time
   - Failed tasks

### Recommended Stack

- **Prometheus** - Metrics collection
- **Grafana** - Dashboards
- **Sentry** - Error tracking

```python
# Add to main.py
from prometheus_client import Counter, Histogram, make_asgi_app

REQUEST_COUNT = Counter('ai_requests_total', 'Total AI requests', ['endpoint', 'status'])
REQUEST_LATENCY = Histogram('ai_request_latency_seconds', 'Request latency')
CLAUDE_TOKENS = Counter('claude_tokens_total', 'Claude API tokens used', ['model'])

# Mount metrics endpoint
metrics_app = make_asgi_app()
app.mount("/metrics", metrics_app)
```

---

## Cost Estimation

### Claude API Costs (per 1M tokens)

| Model            | Input | Output |
| ---------------- | ----- | ------ |
| Claude Haiku 3.5 | $0.25 | $1.25  |
| Claude Sonnet 4  | $3.00 | $15.00 |

### Per-Request Cost (Full Generation)

```
Haiku calls: ~11,000 tokens × $0.00125/1K = $0.014
Sonnet calls: ~21,000 tokens × $0.009/1K = $0.189
---
Total per request: ~$0.20

100 users × 10 requests/day = 1,000 requests/day
Daily cost: ~$200
Monthly cost: ~$6,000
```

### Optimization Impact

With prompt caching (90% reduction on system prompts):

- **Reduced cost: ~$60-80/day = $1,800-2,400/month**

---

## Recommended Immediate Actions

1. **Request Anthropic API tier upgrade** - Critical for 100 users
2. **Add Redis** - For rate limiting and caching
3. **Deploy with Gunicorn multi-worker** - 4x throughput
4. **Implement prompt caching** - 90% token reduction
5. **Add request queuing** - Prevent overload
6. **Monitor with Prometheus/Grafana** - Visibility

---

## Contact Anthropic

For 100+ concurrent users, you should:

1. Email: sales@anthropic.com
2. Request: Enterprise tier or custom rate limits
3. Mention: Expected usage patterns, token volume
