# TITAN: Autonomous AI Business Operations Platform

<div align="center">
  <img src="https://img.shields.io/github/actions/workflow/status/arslanvuzmal/titan/ci-cd.yml?branch=main&label=CI%2FCD&style=for-the-badge&color=success" alt="CI/CD Status" />
  <img src="https://img.shields.io/badge/Python-3.11-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11" />
  <img src="https://img.shields.io/badge/Node.js-20-green?style=for-the-badge&logo=nodedotjs&logoColor=white" alt="Node.js 20" />
  <img src="https://img.shields.io/badge/License-MIT-purple?style=for-the-badge" alt="License MIT" />
</div>

<br />

**TITAN** is an enterprise-grade, event-driven AI operations platform designed to automate complex business workflows with human-in-the-loop oversight. Built for scale, observability, and absolute reliability.

---

## 🏗️ Architecture

The entire system is orchestrated via an event-driven loop backed by Temporal and LangGraph, enabling resilient AI workflows that can pause for human feedback and resume automatically.

```mermaid
graph TD
    %% Define styles
    classDef client fill:#3b82f6,stroke:#2563eb,stroke-width:2px,color:white;
    classDef api fill:#10b981,stroke:#059669,stroke-width:2px,color:white;
    classDef core fill:#8b5cf6,stroke:#7c3aed,stroke-width:2px,color:white;
    classDef data fill:#f59e0b,stroke:#d97706,stroke-width:2px,color:white;
    classDef ext fill:#64748b,stroke:#475569,stroke-width:2px,color:white;

    %% Client Layer
    Dashboard["🖥️ Next.js Dashboard"]:::client
    Webhooks["🪝 External Webhooks"]:::client

    %% API Layer
    FastAPI["⚡ FastAPI Gateway"]:::api
    Auth["🔐 Clerk Auth"]:::api

    %% Core AI & Orchestration Layer
    Temporal["⏳ Temporal.io (Orchestrator)"]:::core
    LangGraph["🧠 LangGraph (AI Agents)"]:::core
    Worker["⚙️ Python Worker Node"]:::core

    %% Data Layer
    Redis["🔴 Redis (Cache & Queue)"]:::data
    Postgres["🐘 PostgreSQL (State)"]:::data
    VectorDB["📊 Qdrant (RAG Vector Store)"]:::data

    %% External
    LLM["🤖 LLM Providers (OpenAI/Anthropic)"]:::ext
    Tools["🛠️ External Tools (Stripe, Slack, etc.)"]:::ext

    %% Connections
    Dashboard <-->|REST/WebSocket| FastAPI
    Webhooks -->|Events| FastAPI
    FastAPI <--> Auth
    
    FastAPI -->|Schedule Workflow| Temporal
    FastAPI <--> Redis
    FastAPI <--> Postgres
    
    Temporal <-->|Dispatch| Worker
    Worker <--> LangGraph
    
    LangGraph <-->|Context Retrieval| VectorDB
    LangGraph <--> Tools
    LangGraph <--> LLM
```

---

## 🚀 Key Features

TITAN is built across a robust **16-step Golden Path** ensuring production readiness:
- **Human-in-the-Loop (HITL):** AI agents automatically suspend execution and request human approval for high-risk actions.
- **Resilient Workflows:** Powered by Temporal, workflows survive server crashes, rate limits, and network partitions with automatic retries.
- **Retrieval-Augmented Generation (RAG):** Context-aware AI agents utilizing a vector database for semantic search and precise answers.
- **Enterprise Security:** JWT-based authentication via Clerk, Role-Based Access Control (RBAC), and secrets management.
- **Full Observability:** Distributed tracing (OpenTelemetry), PromQL metrics, and structured JSON logging.

---

## 🛠️ Tech Stack

| Category | Technologies |
|---|---|
| **Frontend** | Next.js 14, React 19, Tailwind CSS v4, Recharts, Lucide Icons |
| **Backend** | Python 3.11, FastAPI, Pydantic, Prisma Client Python |
| **AI & Orchestration** | LangGraph, LangChain, Temporal.io |
| **Data Infrastructure** | PostgreSQL, Redis, Vector DB (Qdrant) |
| **DevOps & CI/CD** | Docker, Docker Compose, GitHub Actions, GHCR |

---

## 💻 Local Setup

Getting TITAN running locally is frictionless. The entire backing infrastructure runs in Docker.

### 1. Start the Infrastructure
```bash
docker-compose up -d
```
*This spins up PostgreSQL, Redis, and the Temporal Server.*

### 2. Install Dependencies
**Backend (FastAPI)**
```bash
cd apps/api
uv pip install -e .[dev]
```

**Frontend (Next.js)**
```bash
cd apps/web
pnpm install
```

### 3. Run the Development Servers
Open two terminals:
```bash
# Terminal 1: Backend
cd apps/api
uvicorn app.main:app --reload
```

```bash
# Terminal 2: Frontend
cd apps/web
pnpm dev
```

---

## 📸 Screenshots

*(Replace these placeholders with actual screenshots of your dashboard)*

![Dashboard Overview](https://placehold.co/800x400/1e1e2e/ffffff?text=Dashboard+Overview)
*The main operations control center.*

![Execution Trace](https://placehold.co/800x400/1e1e2e/ffffff?text=LangGraph+Execution+Trace)
*Real-time observability into the LangGraph AI decision loops.*
