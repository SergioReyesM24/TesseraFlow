<div align="center">

# TesseraFlow

**Agentes multiusuario en dos capas, tools asíncronas y salidas en tiempo real.**

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![OpenAI](https://img.shields.io/badge/OpenAI-Responses_API-412991?logo=openai&logoColor=white)](https://platform.openai.com/docs/api-reference/responses)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Historial-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Redis-Contexto-DC382D?logo=redis&logoColor=white)](https://redis.io/)

Responses API · protocolo A2A · jobs durables · WebSocket · historial persistente

</div>

---

TesseraFlow es una base de referencia para construir agentes en tiempo real con límites
arquitectónicos claros. Separa un agente interactivo de baja latencia de un agente de
trabajo persistente que ejecuta las tools operativas mediante un protocolo A2A. Conserva
ambas conversaciones en formatos neutrales al proveedor y no acopla el núcleo a OpenAI,
Redis o FastAPI.

> [!NOTE]
> El proyecto todavía no incorpora autenticación. En producción, `user_id` y `tenant_id`
> deben obtenerse de un principal autenticado y no confiarse directamente al cliente.
> Consulta [ROADMAP.md](ROADMAP.md) para conocer las siguientes fases.

## Características

- Arquitectura por capas con dominio y casos de uso independientes del proveedor.
- Agente interactivo aislado de las tools pesadas mediante tres tools de protocolo A2A.
- Conversaciones propias del worker, con historial de tool calls y respuestas entre jobs.
- Cola durable en PostgreSQL con leases, recuperación y orden estricto por thread A2A.
- Consulta de estados públicos `queued`, `running`, `completed` y `failed`.
- Sesiones de modelo aisladas por turno sobre un único cliente HTTP compartido.
- Function calling estricto con argumentos validados por Pydantic.
- Ejecución concurrente de las tools que el worker solicita en una misma respuesta.
- WebSocket persistente con eventos neutrales, varios turnos y correlación por `request_id`.
- Backpressure mediante una cola acotada y un único evento terminal `completed` por turno.
- Historial multiusuario canónico y append-only en PostgreSQL.
- Contexto reciente en Redis con TTL, compactación y recuperación tras cache miss.
- Creación explícita de sesiones con UUID antes de aceptar mensajes.
- Control de propiedad mediante `session_uid`, `user_id` y `tenant_id`.
- Escrituras atómicas y detección de actualizaciones concurrentes.
- Logs estructurados que evitan registrar mensajes y datos de las tools.
- Puertos pequeños para sustituir OpenAI, Redis o las tools sin alterar el núcleo.

## Inicio rápido

### Requisitos

- Python 3.11 o superior.
- PostgreSQL y Redis accesibles.
- Una API key de OpenAI.

### Instalación

```bash
python -m venv .venv
source .venv/bin/activate
make install
cp .env.example .env
```

Configura al menos estas variables en `.env`:

```dotenv
OPENAI_API_KEY=sk-...
POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/tesseraflow
REDIS_URL=redis://localhost:6379/0
```

Inicia ambos servicios con Docker Compose:

```bash
docker compose up -d postgres redis
```

En otra terminal, activa el entorno y arranca la API:

```bash
source .venv/bin/activate
make run
```

La API queda disponible en `http://127.0.0.1:8000` y la documentación interactiva en
[`http://127.0.0.1:8000/docs`](http://127.0.0.1:8000/docs).

### Crear una sesión y enviar el primer mensaje

Cada chat debe comenzar creando una sesión persistida:

```bash
curl -X POST http://127.0.0.1:8000/v1/sessions \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"user-456"}'
```

La respuesta contiene un UUID generado por el servidor:

```json
{"session_uid":"0fda5792-2577-4f26-a56d-71f8dd89ac90"}
```

Utiliza ese UUID para abrir el WebSocket del agente. Por ejemplo, desde un navegador:

```javascript
const sessionUid = "0fda5792-2577-4f26-a56d-71f8dd89ac90";
const socket = new WebSocket(
  `ws://127.0.0.1:8000/v1/agent/ws?session_uid=${sessionUid}&user_id=user-456`,
);

socket.onmessage = ({ data }) => console.log(JSON.parse(data));
socket.onopen = () => socket.send(JSON.stringify({
  type: "message",
  request_id: crypto.randomUUID(),
  message: "¿Cuánto es (125.50 * 3) + 20?",
}));
```

Cada frame del servidor es JSON. El último evento exitoso del turno contiene el
resultado completo:

```json
{
  "type": "completed",
  "request_id": "7a655494-7413-42f2-8e7e-77e3c26b0334",
  "data": {
    "answer": "El resultado es 396.5.",
    "response_id": "resp_...",
    "session_uid": "0fda5792-2577-4f26-a56d-71f8dd89ac90",
    "tool_calls": []
  }
}
```

## Arquitectura

```mermaid
flowchart LR
    Client[Cliente] --> API[FastAPI / WebSocket]
    API --> Service[AgentService]
    Service --> Interactive[Agente interactivo]
    Interactive --> Protocol[Tools A2A]
    Protocol --> Jobs[(Jobs PostgreSQL)]
    Jobs --> Worker[Agente worker]
    Worker --> Tools[Tools operativas]
    Interactive --> Gateway[ModelGateway]
    Worker --> Gateway
    Gateway --> OpenAI[OpenAI Responses API]
    Service --> Repository[ConversationRepository]
    Repository --> Cached[CachedConversationRepository]
    Cached --> PostgreSQL[(PostgreSQL canónico)]
    Cached --> Redis[(Redis contexto reciente)]

    classDef core fill:#e8f5e9,stroke:#198754,color:#102a13
    classDef adapter fill:#eef4ff,stroke:#3776ab,color:#10253f
    class Service,Interactive,Protocol,Worker,Gateway,Repository core
    class API,OpenAI,Tools,Jobs,Cached,PostgreSQL,Redis adapter
```

La dirección de dependencias siempre apunta hacia el núcleo:

```text
api ----------> application <---------- infrastructure
                     |
                     v
                   domain
```

| Capa | Responsabilidad |
| --- | --- |
| `domain` | Conversaciones, eventos, respuestas y tool calls neutrales. |
| `application` | Orquestación interactiva, worker A2A, ciclo de tools y puertos. |
| `infrastructure` | Adaptadores de OpenAI, PostgreSQL, Redis y logging. |
| `api` | Schemas, rutas HTTP/WebSocket y traducción de eventos y errores. |
| `tools` | Capacidades independientes y registro central. |
| `bootstrap.py` | Composición de clientes, adaptadores y servicios concretos. |

`AgentService` solo conoce contratos como `ModelGateway` y `ConversationRepository`.
Los formatos de OpenAI —por ejemplo `function_call_output`— se traducen exclusivamente
en `OpenAIModelSession`.

## Protocolo entre agentes

El agente que habla con el usuario no tiene acceso a `calculator`, `current_time` ni a
otras tools operativas. Solo puede utilizar estas capacidades neutrales:

| Tool A2A | Efecto |
| --- | --- |
| `delegate_to_worker_agent` | Crea un thread y devuelve inmediatamente `thread_id` y `job_id`. |
| `get_worker_agent_status` | Consulta el estado y recupera el informe cuando está completo. |
| `continue_worker_agent` | Añade un mensaje al historial del mismo worker para una ampliación. |

```text
usuario -> agente interactivo -> delegate_to_worker_agent -> queued
                                                        |
                                                        v
PostgreSQL <- worker agent <- tools operativas <- ModelSession propia
     |
     v
get_worker_agent_status -> informe -> agente interactivo -> usuario
```

Cada thread A2A apunta a una conversación interna independiente. El mensaje generado por
el agente interactivo entra en esa conversación con rol `user`; por tanto, el worker lo
trata como un interlocutor humano. Sus respuestas y tool calls quedan persistidas y una
llamada posterior a `continue_worker_agent` crea una nueva sesión de modelo cargando ese
historial. El prompt del worker le exige producir un informe autocontenido y añadir
contexto útil para preguntas posteriores. Cada mensaje usa un envelope JSON
`tesseraflow.a2a` versionado con `message_id`; si el proceso cae después de guardar el
turno pero antes de completar el job, el nuevo worker recupera esa respuesta del
historial en lugar de invocar otra vez al modelo.

Los jobs de un mismo thread se ejecutan en orden. Varios procesos pueden reclamar jobs
distintos con `FOR UPDATE SKIP LOCKED`; una lease vencida permite recuperar trabajo tras
una caída. El worker nunca escribe directamente al WebSocket. Actualmente el resultado
se incorpora cuando el usuario envía otro turno y el agente interactivo consulta el job;
la entrega proactiva mediante outbox permanece en el roadmap.

## Pipeline de conversaciones

El historial se guarda como elementos del dominio, no como respuestas del SDK de un
proveedor:

```text
ConversationMessage | ToolCall | ToolResult
```

El flujo de una interacción es el siguiente:

```mermaid
sequenceDiagram
    participant C as Cliente
    participant A as FastAPI
    participant S as AgentService
    participant R as ConversationRepository
    participant M as ModelSession

    C->>A: message + session_uid + owner
    A->>R: validar sesión y propietario
    A->>S: stream(message, definition, key)
    S->>R: load(ConversationKey)
    R-->>S: Conversation o None
    S->>M: historial neutral + mensaje actual
    M-->>S: respuesta directa o ToolCall A2A
    S->>S: delegar o consultar sin ejecutar trabajo pesado
    M-->>S: respuesta interactiva
    S->>R: save_turn(Conversation, turn)
    R->>R: append canónico y compactación de caché
    R-->>S: Conversation con nueva versión
    S-->>A: AgentResult
    A-->>C: frames JSON hasta completed
```

### El puerto `ConversationRepository`

La aplicación define únicamente estas operaciones:

```python
class ConversationRepository(Protocol):
    async def create(self, key: ConversationKey) -> Conversation: ...
    async def load(self, key: ConversationKey) -> Conversation | None: ...
    async def save_turn(
        self, conversation: Conversation, turn: tuple[ConversationItem, ...]
    ) -> Conversation: ...
    async def delete(self, key: ConversationKey) -> bool: ...
```

`bootstrap.py` conecta el puerto con `CachedConversationRepository`: PostgreSQL es la
fuente de verdad y Redis es una optimización reemplazable. `ConversationService`
gestiona crear, validar y borrar sesiones; `AgentService` se limita a orquestar el
modelo, las tools y la persistencia de cada turno.

### Persistencia canónica en PostgreSQL

La migración `001_conversations.sql` crea dos tablas:

```text
conversations
├── id, user_id, tenant_id
├── title, status, metadata
├── version, last_sequence
└── created_at, updated_at, last_message_at

conversation_items
├── conversation_id, turn_id, sequence
├── item_type, role, call_id, tool_name
└── payload JSONB

a2a_threads
├── parent_conversation_id, worker_conversation_id
└── user_id, tenant_id

a2a_jobs
├── thread_id, sequence, message, status
├── worker_id, lease_expires_at, attempt_count
└── answer, response_id, error_code
```

La identidad persistente interna es `conversation_id`, expuesta por la API como
`session_uid`. No es una sesión del proveedor: cada `ModelSession` pertenece a una sola
ejecución y una conversación atraviesa muchas de esas sesiones. Un UID desconocido
produce `404` y un UID de otro propietario produce `403`.

- Cada interacción añade filas; compactar Redis no elimina historial canónico.
- `turn_id` mantiene juntas las llamadas, resultados y respuesta de un turno.
- `sequence` conserva el orden exacto de todos los elementos.
- Un bloqueo de fila y `version` implementan concurrencia optimista.
- `ON DELETE CASCADE` elimina los elementos al borrar su conversación.
- El primer mensaje genera un título inicial de hasta 120 caracteres.

### Caché de contexto en Redis

Redis almacena `conversation:context:v2:<sha256(conversation_id)>` con la versión, el
propietario, el título y la ventana compactada. Las escrituras usan un script Lua
atómico que impide que una carga antigua sobrescriba una versión nueva. Si Redis expira
o falla, el contexto reciente se reconstruye desde PostgreSQL; un fallo de caché no
invalida una escritura canónica exitosa.

La conversación se persiste después de obtener la respuesta final. En streaming se
guarda antes de emitir el evento terminal `completed`, por lo que un stream exitoso ya
tiene su historial retenido.

## Protocolo WebSocket

La conexión queda asociada a una conversación y a su propietario durante el handshake:

```text
ws://127.0.0.1:8000/v1/agent/ws?session_uid=<uuid>&user_id=<owner>&tenant_id=<tenant>
```

Tras el evento `connected`, el cliente puede enviar varios turnos por la misma conexión:

```json
{
  "type": "message",
  "request_id": "7a655494-7413-42f2-8e7e-77e3c26b0334",
  "message": "¿Cuánto es 125.50 multiplicado por 3?"
}
```

`request_id` es opcional; si falta, el servidor genera un UUID. Todos los eventos de un
turno repiten ese identificador. Los turnos recibidos por una conexión se procesan en
orden y la cola admite como máximo ocho pendientes.

El protocolo público utiliza eventos neutrales al proveedor:

| Evento | Significado |
| --- | --- |
| `connected` | Confirma la conexión e informa `connection_id` y `session_uid`. |
| `text_delta` | Fragmento incremental del texto. |
| `tool_started` | Una tool validada está a punto de ejecutarse. |
| `tool_completed` | Resultado, estado, duración y posible error de la tool. |
| `completed` | Resultado final; siempre es el último evento exitoso. |
| `error` | El stream no pudo completarse; los detalles internos quedan en logs. |

```json
{
  "type": "text_delta",
  "request_id": "7a655494-7413-42f2-8e7e-77e3c26b0334",
  "data": {"text": "El resultado"}
}
```

Los argumentos fragmentados se acumulan dentro del adaptador antes de exponer un
`ToolCall`. Si el cliente se desconecta, se cancela el generador y se cierra únicamente
el stream de ese turno; el cliente compartido permanece disponible. Los frames inválidos
y los fallos de un turno producen un evento `error` seguro sin cerrar necesariamente el
socket.

`POST /v1/agent/stream` continúa disponible temporalmente como transporte SSE de
compatibilidad. Los nuevos clientes deben usar el WebSocket.

## Endpoints

| Método | Ruta | Descripción |
| --- | --- | --- |
| `GET` | `/health` | Liveness check sin consultar dependencias externas. |
| `POST` | `/v1/sessions` | Crea una sesión vacía y devuelve su `session_uid`. |
| `WS` | `/v1/agent/ws` | Mantiene una conversación y transmite turnos mediante frames JSON. |
| `POST` | `/v1/agent/stream` | Transporte SSE conservado durante la migración. |
| `DELETE` | `/v1/conversations/{conversation_id}` | Borra una conversación del propietario indicado. |

El endpoint de borrado recibe `user_id` y, opcionalmente, `tenant_id` como query params:

```bash
curl -X DELETE \
  'http://127.0.0.1:8000/v1/conversations/conv-123?user_id=user-456'
```

## Tools incluidas

| Tool | Capacidad |
| --- | --- |
| `calculator` | Suma, resta, multiplica y divide números decimales. |
| `current_time` | Devuelve fecha y hora para una zona horaria IANA. |

### Añadir una tool

1. Define un modelo de argumentos que herede de `ToolArguments`.
2. Implementa una clase `AgentTool` con una única capacidad.
3. Registra una instancia en `build_tool_registry()`.

```python
from typing import ClassVar

from pydantic import Field

from application.tools import AgentTool, ToolArguments, ToolExecutionContext


class CustomerInput(ToolArguments):
    """Arguments required to retrieve one customer."""

    customer_id: str = Field(description="Identificador interno del cliente")


class GetCustomerTool(AgentTool[CustomerInput]):
    """Retrieve the basic state of one customer."""

    name = "get_customer"
    description = "Obtiene los datos básicos de un cliente."
    arguments_model: ClassVar[type[CustomerInput]] = CustomerInput

    async def execute(
        self,
        arguments: CustomerInput,
        context: ToolExecutionContext,
    ) -> object:
        """Return the customer state for the validated identifier."""
        del context
        return {"customer_id": arguments.customer_id, "status": "active"}
```

El esquema neutral se genera desde Pydantic y cada gateway lo traduce al formato de su
proveedor. La validación, la medición, los logs y el tratamiento de errores son comunes
a todas las tools.

## Configuración

| Variable | Valor por defecto | Propósito |
| --- | --- | --- |
| `OPENAI_API_KEY` | — | Credencial de OpenAI. |
| `OPENAI_BASE_URL` | — | Base URL alternativa compatible. |
| `OPENAI_MODEL` | `gpt-5-mini` | Modelo usado por la definición por defecto. |
| `WORKER_AGENT_MODEL` | igual que `OPENAI_MODEL` | Modelo del agente de trabajo. |
| `OPENAI_CONNECT_TIMEOUT_SECONDS` | `15` | Timeout de conexión. |
| `POSTGRES_URL` | `postgresql://.../tesseraflow` | Fuente canónica de conversaciones. |
| `POSTGRES_POOL_MIN_SIZE` | `1` | Conexiones mínimas por proceso. |
| `POSTGRES_POOL_MAX_SIZE` | `10` | Conexiones máximas por proceso. |
| `POSTGRES_COMMAND_TIMEOUT_SECONDS` | `30` | Timeout de comandos SQL. |
| `REDIS_URL` | `redis://localhost:6379/0` | Caché de contexto reciente. |
| `MAX_TOOL_ROUNDS` | `8` | Límite contra bucles de tools. |
| `A2A_WORKER_POLL_SECONDS` | `0.5` | Intervalo de sondeo de la cola durable. |
| `A2A_JOB_TIMEOUT_SECONDS` | `600` | Tiempo máximo de un turno del worker. |
| `CONVERSATION_TTL_SECONDS` | `604800` | TTL de la caché; no borra PostgreSQL. |
| `CONVERSATION_MAX_MESSAGES` | `100` | Máximo de elementos en el contexto reciente. |
| `CONVERSATION_MAX_CHARACTERS` | `200000` | Límite lógico del contexto reciente. |
| `CONVERSATION_MAX_BYTES` | `512000` | Límite del JSON guardado en Redis. |
| `LOG_LEVEL` | `INFO` | Nivel de logging. |
| `LOG_JSON` | `false` | Activa logs JSON estructurados. |

Consulta [.env.example](.env.example) para ver una configuración completa.

## Ciclos de vida y concurrencia

- `AsyncOpenAI`, el pool PostgreSQL y el cliente Redis se crean una vez por proceso
  durante el `lifespan` y se cierran durante el apagado.
- Cada ejecución crea una `ModelSession` ligera con su propio estado efímero.
- No se guarda usuario, historial ni `response_id` en servicios compartidos.
- El worker se inicia y detiene con el `lifespan`; una interrupción libera su job.
- Los jobs sobreviven a reinicios y una lease vencida permite reclamarlos de nuevo.
- Dos jobs del mismo thread nunca se ejecutan a la vez.
- Un lector independiente detecta el cierre del WebSocket y cancela el turno activo.
- Los turnos de una conexión se procesan secuencialmente y su cola es acotada.
- Las tools operativas solo se exponen al worker. Si solicita varias, actualmente se
  ejecutan concurrentemente y sus resultados se devuelven juntos.
- Las cancelaciones se propagan y los streams se liberan mediante context managers.

## Seguridad y límites actuales

- Los logs no incluyen mensajes, argumentos, resultados, claves ni datos sensibles.
- Los errores esperables de una tool se convierten en resultados estructurados.
- Los historiales no se resumen automáticamente: se eliminan turnos antiguos completos
  para evitar alterar información sensible.
- Mensajes, argumentos y resultados sí forman parte del historial persistido. El
  cifrado, la clasificación de datos y la retención legal dependen del despliegue.
- Esta base no implementa todavía autenticación, autorización externa, cifrado a nivel
  de aplicación ni políticas regulatorias específicas.

## Desarrollo y calidad

Ejecuta todas las comprobaciones locales con:

```bash
make check
```

O de forma individual:

```bash
ruff format --check .
ruff check .
mypy src
pytest
git diff --check
```

La suite normal no necesita API keys, red ni servicios externos.

## Estructura del proyecto

```text
src/
├── api/                 # FastAPI, schemas, WebSocket y SSE de compatibilidad
├── application/         # Casos de uso, puertos y orquestación
├── domain/              # Modelos y eventos neutrales
├── infrastructure/      # OpenAI, PostgreSQL, Redis y logging
├── tools/               # Tools concretas y registro
├── bootstrap.py         # Composición de dependencias
└── main.py              # Aplicación y lifespan
tests/                   # Tests unitarios de casos de uso y adaptadores
```

## Roadmap

Las capacidades futuras y las decisiones pendientes se mantienen en
[ROADMAP.md](ROADMAP.md). Esto evita presentar como disponible una funcionalidad que
todavía no está implementada.
