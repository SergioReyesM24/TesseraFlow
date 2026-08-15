<div align="center">

# TesseraFlow

**Agentes multiusuario en dos capas, tools asíncronas y salidas en tiempo real.**

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Gemini](https://img.shields.io/badge/Gemini-Live_API-4285F4?logo=google&logoColor=white)](https://ai.google.dev/gemini-api/docs/live-api)
[![OpenAI](https://img.shields.io/badge/OpenAI-Responses_API-412991?logo=openai&logoColor=white)](https://platform.openai.com/docs/api-reference/responses)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Historial-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Redis-Contexto-DC382D?logo=redis&logoColor=white)](https://redis.io/)

Gemini Live · Responses API · protocolo A2A · jobs durables · WebSocket

</div>

---

TesseraFlow es una base de referencia para construir agentes en tiempo real con límites
arquitectónicos claros. Separa un agente interactivo de baja latencia de un agente de
trabajo persistente que ejecuta las tools operativas mediante un protocolo A2A. Conserva
ambas conversaciones en formatos neutrales al proveedor. En la configuración predeterminada,
Gemini 3.1 Flash Live produce la conversación y el audio dirigido al usuario, mientras
OpenAI ejecuta el agente de trabajo pesado.

> [!NOTE]
> La autenticación queda fuera del alcance de este repositorio. En un despliegue que la
> necesite, `user_id` debe obtenerse en una capa externa y no confiarse directamente al
> cliente. Consulta [ROADMAP.md](ROADMAP.md) para conocer las siguientes fases.

Consulta también las [preguntas frecuentes](FAQ.md) para conocer el comportamiento de
los jobs, el historial y las colas de los canales textual y realtime.

## Núcleo: doble capa multimodal

El núcleo de TesseraFlow no es un transporte ni un proveedor concreto, sino una
arquitectura estable de dos agentes. El primer agente mantiene la interacción de baja
latencia con el usuario y solo conoce tools de protocolo A2A. El segundo trabaja de forma
durable, conserva su propio contexto y ejecuta las tools operativas. Texto, WebSocket
durable y voz realtime son distintas puertas de entrada a esa misma separación.

```text
Texto por SSE ───────────┐
Texto por WS durable ────┼──> Agente interactivo ──> Tools A2A ──> Worker ──> Tools pesadas
Audio STS realtime ──────┘
```

| Forma de interacción | Primera capa | Segunda capa | Entrega del worker |
| --- | --- | --- | --- |
| `POST /v1/agent/stream` | Agente por turnos; entrada textual y salida según el modelo configurado | Worker textual durable | El job continúa, pero el SSE inicial no permanece abierto para su resultado posterior |
| `WS /v1/agent/ws` | Agente por turnos con comandos y outputs durables | Worker textual durable | Proactiva mediante inbox/outbox en `/v1/agent/ws`, incluso después del turno inicial |
| `WS /v1/agent/realtime` | Agente STS full-duplex independiente del proveedor | El mismo worker textual durable | Proactiva en la sesión de voz mediante inbox durable y escritor único |

Las tres entradas comparten contratos neutrales, tools A2A, historial, control de
propiedad y worker, pero texto y realtime tienen proveedor, modelo y definición propios.
Los comandos por turnos priorizan durabilidad; STS prioriza latencia y mantiene el audio
crudo fuera de la persistencia.

## Características

- Arquitectura por capas con dominio y casos de uso independientes del proveedor.
- Agente interactivo aislado de las tools pesadas mediante tres tools de protocolo A2A.
- Conversaciones propias del worker, con historial de tool calls y respuestas entre jobs.
- Cola durable en PostgreSQL con leases, recuperación y orden estricto por thread A2A.
- Inbox por conversación y modo que serializa mensajes y finalizaciones del worker.
- Entrega proactiva mediante outbox textual o inyección en la sesión STS activa.
- Consulta de estados públicos `queued`, `running`, `completed` y `failed`.
- Sesiones realtime STS aisladas por WebSocket sobre clientes compartidos por proveedor.
- Audio PCM nativo a 24 kHz con transcripción textual y eventos de interrupción neutrales.
- STS bidireccional con formatos anunciados por el adaptador, VAD neutral y barge-in.
- Proveedor y modelo configurables por rol; un mismo proveedor comparte cliente y pool.
- Function calling estricto con argumentos validados por Pydantic.
- Ejecución concurrente de las tools que el worker solicita en una misma respuesta.
- WebSocket persistente con eventos neutrales, varios turnos y correlación por `request_id`.
- Un único evento terminal `completed` o `error` por comando aceptado.
- Historial multiusuario canónico y append-only en PostgreSQL.
- Contexto reciente en Redis con TTL, compactación y recuperación tras cache miss.
- Creación explícita de sesiones con UUID antes de aceptar mensajes.
- Control de propiedad mediante `session_uid` y `user_id`.
- Escrituras atómicas y detección de actualizaciones concurrentes.
- Logs estructurados que evitan registrar mensajes y datos de las tools.
- Puertos pequeños para sustituir Gemini, OpenAI, Redis o las tools sin alterar el núcleo.

## Inicio rápido

### Requisitos

- Python 3.11 o superior.
- PostgreSQL y Redis accesibles.
- Una API key de Gemini para el STS predeterminado, o de OpenAI para `gpt-realtime-2.1`.
- Una API key de OpenAI para los agentes textuales.

### Instalación

```bash
cp .env.example .env
```

Configura al menos estas variables en `.env`:

```dotenv
GEMINI_API_KEY=...
OPENAI_API_KEY=sk-...
POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/tesseraflow
REDIS_URL=redis://localhost:6379/0
```

Para levantar todo con Docker Compose no hace falta cambiar `POSTGRES_URL` ni `REDIS_URL`
en tu `.env`: el compose los sobreescribe dentro del contenedor del backend para apuntar
a los servicios `postgres` y `redis`.

```bash
docker compose up --build
```

El cliente web queda disponible en `http://127.0.0.1:5173`, la API en
`http://127.0.0.1:8000` y la documentación interactiva en
[`http://127.0.0.1:8000/docs`](http://127.0.0.1:8000/docs).

Si prefieres ejecutar la API localmente y usar Docker solo para PostgreSQL y Redis:

```bash
python -m venv .venv
source .venv/bin/activate
make install
docker compose up -d postgres redis
make run
```

### Cliente web React

El directorio `frontend/` contiene una interfaz React + TypeScript para los dos
WebSockets. El modo **Texto** consume `/v1/agent/ws` como un chat persistente; el modo
**Voz** usa `/v1/agent/realtime`, captura el micrófono como PCM16 mono a la frecuencia
negociada por el adaptador (16 kHz con Gemini, 24 kHz con OpenAI), reproduce la respuesta
PCM16 conforme llega y muestra las transcripciones de ambos interlocutores. El
modo **Historial técnico** consulta por `session_uid` los registros canónicos, agrupados
por `turn_id`, y muestra los argumentos y resultados de cada tool call. Primero enumera
únicamente las conversaciones principales del usuario. Al seleccionar una, consulta su
grupo y permite alternar entre el historial principal y los historiales internos de cada
worker, cada uno con sus propios mensajes, orden y paginación.

Con la API levantada en `http://127.0.0.1:8000`, inicia el cliente en otra terminal:

```bash
cd frontend
npm install
npm run dev
```

Abre `http://127.0.0.1:5173`. El proxy de Vite reenvía HTTP y WebSocket a la API local.
Para usar otro backend, copia `frontend/.env.example` a `frontend/.env` y cambia
`TESSERAFLOW_BACKEND_URL`; también puedes indicar una URL desde la configuración de la
interfaz. El micrófono requiere un contexto seguro (`https` o `localhost`); el endpoint
realtime está disponible siempre que su proveedor y modelo configurados sean válidos.

El cliente crea una sesión mediante `POST /v1/sessions` al cargar y cada vez que se pulsa
**Nueva conversación**. El `user_id` de demostración y la URL de conexión se guardan solo
como preferencias locales; los mensajes y el audio no se escriben en el almacenamiento
del navegador.

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

socket.onmessage = ({ data }) => {
  const event = JSON.parse(data);
  if (event.type === "audio_delta") {
    // PCM16 mono a 24 kHz. Encola los bytes en tu reproductor Web Audio.
    const pcm = Uint8Array.from(atob(event.data.audio), (byte) => byte.charCodeAt(0));
    playPcm16(pcm, 24000);
  } else if (event.type === "audio_interrupted") {
    clearPlaybackQueue();
  }
  console.log(event);
};
socket.onopen = () => socket.send(JSON.stringify({
  type: "message",
  request_id: crypto.randomUUID(),
  message: "¿Cuánto es (125.50 * 3) + 20?",
}));
```

Para micrófono bidireccional usa el endpoint realtime, que siempre está compuesto. Con el
adaptador seleccionado anuncia ambos formatos en `realtime_ready`: Gemini recibe PCM16
mono little-endian a 16 kHz y OpenAI a 24 kHz; ambos emiten PCM16 mono a 24 kHz:

```javascript
const realtime = new WebSocket(
  `ws://127.0.0.1:8000/v1/agent/realtime?session_uid=${sessionUid}&user_id=user-456`,
);
realtime.binaryType = "arraybuffer";

realtime.onmessage = ({ data }) => {
  if (data instanceof ArrayBuffer) {
    playPcm16(new Uint8Array(data), 24000);
    return;
  }
  const event = JSON.parse(data);
  if (event.type === "audio_interrupted") clearPlaybackQueue();
  console.log(event);
};

realtime.onopen = () => {
  realtime.send(JSON.stringify({ type: "audio_start", turn_id: crypto.randomUUID() }));
};
const sendMicChunk = (pcm16Chunk) => realtime.send(pcm16Chunk);
const stopMic = () => realtime.send(JSON.stringify({ type: "audio_end" }));
```

En `/v1/agent/ws`, cada frame del servidor es JSON. El último evento exitoso del turno
contiene el resultado completo:

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
    Client --> RealtimeAPI[WebSocket realtime PCM]
    API --> Inbox[(Interaction inbox)]
    Inbox --> Coordinator[ConversationCoordinator]
    Coordinator --> Interactive[TurnInteractionAgent]
    Interactive --> Protocol[Tools A2A]
    Protocol --> Jobs[(Jobs PostgreSQL)]
    Jobs --> Worker[Agente worker]
    Worker --> Inbox
    Worker --> Tools[Tools operativas]
    Interactive --> InteractiveService[AgentService]
    InteractiveService --> InteractiveGateway[ModelGateway]
    InteractiveGateway --> OpenAI[OpenAI APIs]
    RealtimeAPI --> RealtimeService[RealtimeAgentService]
    RealtimeService --> RealtimeGateway[RealtimeModelGateway]
    RealtimeGateway --> Gemini[Gemini Live API]
    RealtimeGateway --> OpenAIRealtime[OpenAI Realtime API]
    RealtimeService --> Protocol
    Worker --> WorkerService[AgentService]
    WorkerService --> WorkerGateway[ModelGateway]
    WorkerGateway --> OpenAI
    Coordinator --> Outbox[(Interaction outbox)]
    Outbox --> API
    InteractiveService --> Repository[ConversationRepository]
    RealtimeService --> Repository
    WorkerService --> Repository
    Repository --> Cached[CachedConversationRepository]
    Cached --> PostgreSQL[(PostgreSQL canónico)]
    Cached --> Redis[(Redis contexto reciente)]

    classDef core fill:#e8f5e9,stroke:#198754,color:#102a13
    classDef adapter fill:#eef4ff,stroke:#3776ab,color:#10253f
    class Coordinator,Interactive,InteractiveService,RealtimeService,Protocol,Worker,WorkerService,Repository core
    class API,RealtimeAPI,Inbox,Outbox,InteractiveGateway,RealtimeGateway,WorkerGateway,Gemini,OpenAI,OpenAIRealtime,Tools,Jobs,Cached,PostgreSQL,Redis adapter
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
| `infrastructure` | Adaptadores de Gemini Live, OpenAI, PostgreSQL, Redis y logging. |
| `api` | Schemas, rutas HTTP/WebSocket y traducción de eventos y errores. |
| `tools` | Capacidades independientes y registro central. |
| `bootstrap.py` | Composición de clientes, adaptadores y servicios concretos. |

`AgentService` orquesta ejecuciones por turnos para interacción textual, audio con entrada
textual y trabajo pesado. `RealtimeAgentService` representa la semántica distinta de una
conexión full-duplex: audio concurrente, varios turnos VAD, interrupciones y cierre ligado
al socket. Ambos comparten `ToolExecutor`, contratos de tools, `AgentDefinition` y el
repositorio neutral. Cada `ModelSession` o `RealtimeModelSession` concreto conserva los
tipos y estados de su SDK exclusivamente en `infrastructure`.

`ModelRuntime` selecciona y crea gateways, definiciones y clientes desde la configuración,
comparte un cliente cuando dos roles usan el mismo proveedor y encapsula su cierre. El
`bootstrap.py` solo recibe servicios neutrales y no importa ningún SDK de modelos.

El endpoint textual abre sesiones turn-based aisladas. El endpoint realtime mantiene una
conexión STS durante la vida del WebSocket: el audio cruza de forma efímera y las
transcripciones, tool calls, resultados y respuestas proactivas se guardan por turno.

## Protocolo entre agentes

El agente que habla con el usuario no tiene acceso a las tools operativas. Solo puede
utilizar estas capacidades neutrales:

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
comando worker_completed con delivery_mode
     |-- turn_based -> coordinador -> agente textual -> outbox -> usuario
     `-- realtime   -> sesión STS activa -> audio/transcripción -> usuario
```

Cada thread A2A apunta a una conversación interna independiente. El mensaje generado por
el agente interactivo entra en esa conversación con rol `user`; por tanto, el worker lo
trata como un interlocutor humano. Sus respuestas y tool calls quedan persistidas y una
llamada posterior a `continue_worker_agent` crea una nueva sesión de modelo cargando ese
historial interno, sin cargar el historial de la conversación principal. La cabecera de
la conversación interna, el thread y su primer job se crean en una única transacción para
que no puedan quedar sesiones internas huérfanas.

El prompt del worker le exige producir un informe autocontenido y añadir
contexto útil para preguntas posteriores. Cada mensaje usa un envelope JSON
`tesseraflow.a2a` versionado con `message_id`; si el proceso cae después de guardar el
turno pero antes de completar el job, el nuevo worker recupera esa respuesta del
historial en lugar de invocar otra vez al modelo.

Los jobs de un mismo thread se ejecutan en orden. Un `NOTIFY` despierta los workers al
insertar, reencolar o terminar trabajo —un estado terminal puede desbloquear el siguiente
job del thread— y una reconciliación periódica recupera señales perdidas y leases vencidas.
Varios procesos pueden reclamar jobs distintos con `FOR UPDATE SKIP LOCKED`. Al terminar,
el repositorio cambia el estado del job y crea un comando
`worker_completed` en una sola operación SQL. El worker nunca llama al modelo principal
ni escribe directamente al WebSocket.

Cada job conserva el `delivery_mode` de la ejecución que lo creó. El
`ConversationCoordinator` solo reclama comandos `turn_based`; el resultado A2A abre un
turno nuevo del agente textual y sus eventos se guardan en el outbox antes de entregarse.
La sesión realtime solo reclama comandos `realtime` de su conversación y propietario
cuando el VAD del proveedor no tiene un turno activo. La captura del micrófono puede seguir
abierta mientras el usuario está en silencio. Si no existe socket, el comando permanece
durable hasta la siguiente conexión. Se inyecta el envelope versionado
`tesseraflow.a2a.result` en la sesión STS, y el claim se confirma únicamente después de
persistir el evento terminal real del proveedor. Por tanto una finalización realtime no
puede arrancar el agente textual ni crear una respuesta assistant fantasma.

La precedencia por `sequence` se aplica dentro de cada modo. Las leases y
`FOR UPDATE SKIP LOCKED` impiden que dos sockets consuman el mismo comando; una
desconexión o cancelación lo devuelve a la inbox.

Los inserts de jobs, inbox y outbox emiten señales PostgreSQL `NOTIFY` después del commit. Una
única conexión `LISTEN` por proceso distribuye esos avisos a los consumidores locales por
`command_id` o conversación. El aviso solo despierta: cada consumidor vuelve a consultar
la fila durable y conserva una reconciliación periódica lenta para recuperarse de una
notificación perdida o una conexión reiniciada.

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
    participant I as Interaction inbox
    participant K as ConversationCoordinator
    participant S as AgentService
    participant R as ConversationRepository
    participant M as ModelSession

    C->>A: message + session_uid + owner
    A->>R: validar sesión y propietario
    A->>I: enqueue(text_user)
    I-->>K: NOTIFY comando disponible
    K->>I: claim_next por conversación
    K->>S: stream(command.message, source)
    S->>R: load(ConversationKey)
    R-->>S: Conversation o None
    S->>M: historial neutral + mensaje actual
    M-->>S: respuesta directa o ToolCall A2A
    S->>S: delegar o consultar sin ejecutar trabajo pesado
    M-->>S: respuesta interactiva
    S->>R: save_turn(Conversation, turn)
    R->>R: append canónico y compactación de caché
    R-->>S: Conversation con nueva versión
    S-->>K: eventos neutrales
    K->>I: append outbox + complete command
    I-->>A: NOTIFY output disponible
    I-->>A: eventos pendientes
    A-->>C: frames JSON hasta completed/error
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

Las tablas funcionales se agrupan en tres responsabilidades principales:

```text
1. Conversaciones
   ├── conversations
   └── conversation_items

2. Trabajo entre agentes
   ├── a2a_threads
   └── a2a_jobs

3. Coordinación y entrega al usuario
   ├── interaction_commands
   └── interaction_outbox
```

Además existe `schema_migrations`, una tabla técnica que solo registra qué migraciones
SQL se han aplicado. No contiene conversaciones, mensajes ni estado de los agentes.

| Tabla | Responsabilidad | Contenido principal |
| --- | --- | --- |
| `conversations` | Cabecera e identidad de una conversación. | Propietario, versión y título. |
| `conversation_items` | Historial que recibe el modelo. | Mensajes, tool calls y tool results. |
| `a2a_threads` | Relación estable entre una conversación principal y una conversación del worker. | `parent_conversation_id` y `worker_conversation_id`. |
| `a2a_jobs` | Cola y estado de tareas pesadas. | Mensaje, modo de entrega, estado, lease y resultado. |
| `interaction_commands` | Inbox durable separada por modo de entrega. | Mensajes del usuario y finalizaciones del worker. |
| `interaction_outbox` | Eventos pendientes de entregar al cliente. | Deltas, estados de tools, respuestas finales y errores. |

Las migraciones `001_conversations.sql`, `002_a2a_jobs.sql` y
`003_interaction_inbox_outbox.sql` crean esas tablas por bloques. La migración
`004_interaction_notifications.sql` añade triggers `LISTEN/NOTIFY` sin convertir las
notificaciones en fuente de verdad. `005_interaction_audio_events.sql` amplía el outbox
para persistir los eventos neutrales `audio_delta` y `audio_interrupted`;
`006_a2a_job_notifications.sql` despierta workers A2A y
`007_interaction_delivery_modes.sql` separa los claims `turn_based` y `realtime`, con
`turn_based` como valor para filas existentes. `008_a2a_conversation_correlation.sql`
convierte `a2a_threads` en la única autoridad de la relación y rechaza propietarios
distintos, ciclos y conversaciones con roles ambiguos. El `user_id` redundante se conserva
por compatibilidad de despliegue, pero se valida contra `conversations` y las consultas no
lo tratan como autoridad. Su estructura principal es la siguiente:

```text
conversations
├── id, user_id
├── title, status, metadata
├── version, last_sequence
└── created_at, updated_at, last_message_at

conversation_items
├── conversation_id, turn_id, sequence
├── item_type, role, call_id, tool_name
└── payload JSONB

a2a_threads
├── parent_conversation_id, worker_conversation_id
└── user_id (proyección validada para compatibilidad)

a2a_jobs
├── thread_id, sequence, message, delivery_mode, status
├── worker_id, lease_expires_at, attempt_count
└── answer, response_id, error_code

interaction_commands
├── conversation_id, request_id, kind, source, message, delivery_mode
├── sequence, status, worker_id, lease_expires_at
└── causation_id, attempt_count, error_code

interaction_outbox
├── command_id, conversation_id, request_id, modality
├── sequence, event_type, payload JSONB
└── delivered_at
```

La identidad persistente interna es `conversation_id`, expuesta por la API como
`session_uid`. No es una sesión del proveedor: cada `ModelSession` pertenece a una sola
ejecución y una conversación atraviesa muchas de esas sesiones. Un UID desconocido
produce `404` y un UID de otro propietario produce `403`.

La API deriva la agrupación desde `a2a_threads`; no almacena otro identificador raíz. El
listado paginado de sesiones devuelve solo conversaciones principales y expone `total` junto
con `has_more` y `next_offset`, de modo que el cliente puede calcular la última página sin
cargar las anteriores. Tanto sus elementos como el historial incluyen una proyección `correlation`, y
`GET /v1/sessions/{session_uid}/group` acepta el ID principal o uno interno y devuelve el
grupo completo sin mezclar historiales. Los identificadores tienen esta semántica:

| Identificador | Semántica |
| --- | --- |
| `conversation_id` | Historial físico aislado; cada worker usa uno distinto del principal. |
| `root_conversation_id` | Proyección consultable del ID principal; se deriva, no se persiste. |
| `parent_conversation_id` | Conversación principal a la que pertenece un worker; es nulo para la principal. |
| `worker_conversation_id` | ID del historial interno; coincide con su `conversation_id`. |
| `thread_id` | Relación estable principal-worker que admite varios jobs ordenados. |
| `job_id` | Mensaje durable concreto dentro de un thread A2A. |
| `request_id` | Ejecución lógica de un comando; en una finalización A2A coincide con `job_id`. |
| `turn_id` | Agrupa los elementos persistidos de un turno dentro de un `conversation_id`; para A2A coincide con `job_id` en el worker y en la posterior entrega al principal. |

El `X-Request-ID` HTTP se registra como `http_request_id`, por lo que no se confunde con
el `request_id` lógico de una interacción. Los logs del worker incluyen además
`conversation_id`, `parent_conversation_id`, `worker_conversation_id`, `thread_id`,
`job_id` y `turn_id`, sin contenido de mensajes.

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
ws://127.0.0.1:8000/v1/agent/ws?session_uid=<uuid>&user_id=<owner>
```

Tras el evento `connected`, el cliente puede enviar varios turnos por la misma conexión:

```json
{
  "type": "message",
  "request_id": "7a655494-7413-42f2-8e7e-77e3c26b0334",
  "message": "¿Cuánto es 125.50 multiplicado por 3?"
}
```

`request_id` es opcional; si falta, el servidor genera un UUID. No forma parte de la URL
ni identifica la conexión: únicamente correlaciona un mensaje aceptado con sus eventos.
La aplicación genera además un `command_id` interno. Todos los comandos de una
conversación —incluidos los resultados A2A— se procesan por `sequence` en PostgreSQL.
Las respuestas proactivas usan el `job_id` como `request_id`, de modo que el cliente puede
relacionarlas con el identificador devuelto al delegar.

El protocolo público utiliza eventos neutrales al proveedor:

| Evento | Significado |
| --- | --- |
| `connected` | Confirma la conexión e informa `connection_id` y `session_uid`. |
| `audio_delta` | PCM16 mono codificado en base64 a la frecuencia anunciada. |
| `audio_interrupted` | Obliga a vaciar el buffer de reproducción pendiente. |
| `text_delta` | Fragmento incremental del texto. |
| `tool_started` | Una tool validada está a punto de ejecutarse. |
| `tool_completed` | Resultado, estado, duración y posible error de la tool. |
| `visual_component` | Componente semántico v1 validado: gráfica, métricas o movimientos financieros. |
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
`ToolCall`. Si el cliente se desconecta, el comando aceptado continúa fuera del ciclo de
vida del socket. Sus salidas no confirmadas permanecen en el outbox y se entregan al
reconectar a la misma conversación. Los frames inválidos y los fallos de un comando
producen un evento `error` seguro sin exponer detalles internos.

`POST /v1/agent/stream` continúa disponible temporalmente como transporte SSE de
compatibilidad. Los nuevos clientes deben usar el WebSocket.

### Componentes visuales

Los componentes visuales son un output de aplicación independiente del modelo. Cualquier
tool puede devolver `ToolExecutionOutput(value=..., visual_components=(...))`: `value`
continúa hacia el modelo como resultado normal y el executor captura cada presentación,
la valida y publica directamente un evento neutral al frontend. No hace falta una segunda
tool call a `present_visual`.

El envelope público es general y versionado (`schema`, `version`, `component_id`,
`fallback_text`, `component`). El catálogo de componentes dentro del envelope sigue siendo
semántico y cerrado para que el frontend nunca ejecute markup o configuración arbitraria:

```json
{
  "type": "visual_component",
  "request_id": "7a655494-7413-42f2-8e7e-77e3c26b0334",
  "data": {
    "schema": "tesseraflow.visual",
    "version": 1,
    "component_id": "weekly-balance",
    "fallback_text": "El saldo termina el periodo en 13.275,65 €.",
    "component": {
      "kind": "chart",
      "title": "Saldo semanal",
      "subtitle": "Últimas dos semanas",
      "chart_type": "line",
      "x_axis": {"label": "Semana"},
      "y_axis": {"label": "Saldo", "unit": "€"},
      "series": [
        {
          "name": "Saldo al cierre",
          "points": [
            {"x": "12-07-2026", "y": 13450.0},
            {"x": "19-07-2026", "y": 13275.65}
          ]
        }
      ]
    }
  }
}
```

La versión 1 permite gráficas `line`/`bar`, grupos `metric_group` y listas
`transaction_list`. Estas últimas muestran ahorro base y actual, ingresos, gastos y
movimientos con comercio, categoría, cantidad y saldo resultante. Los títulos, series,
métricas, transacciones y cantidades de puntos tienen límites explícitos; los campos
desconocidos, números no finitos y tipos no registrados se rechazan. El contrato no acepta
HTML, CSS, JavaScript, callbacks ni nombres de componentes React. Tanto el WebSocket
textual como el realtime exponen el mismo evento, y el SSE de compatibilidad utiliza el
mismo codec. Un cliente que no soporte el tipo o la versión debe mostrar `fallback_text`;
la respuesta textual completa sigue llegando normalmente.

Las presentaciones viajan también en los resultados A2A: si una tool del worker genera una,
el backend la incorpora al comando de finalización y la emite en los canales textual y
realtime antes de la respuesta del agente principal. `present_visual` se conserva para que
el usuario pueda pedir otro formato; al reutilizar `component_id`, el nuevo componente
reemplaza al anterior en lugar de duplicarlo.

### WebSocket speech-to-speech

El endpoint STS habilita una conexión distinta:

```text
ws://127.0.0.1:8000/v1/agent/realtime?session_uid=<uuid>&user_id=<owner>
```

Después de `connected` y `realtime_ready`, el cliente abre la captura con
`{"type":"audio_start","turn_id":"<uuid>"}`, envía frames binarios PCM16 y la pausa
con `{"type":"audio_end"}`. El adaptador seleccionado usa VAD, de modo que un mismo flujo puede
producir varios `turn_completed`. Los turnos posteriores reciben un `turn_id` generado por
el servidor. También se admite `{"type":"text","turn_id":"<uuid>","text":"..."}` como
degradación textual dentro de la misma sesión.

El servidor devuelve audio como frames binarios sin base64. El resto son frames JSON:

| Evento | Significado |
| --- | --- |
| `realtime_ready` | Informa formatos, actividad y recuperación soportados por el adaptador. |
| `audio_started` / `audio_ended` | Confirma los límites del flujo de captura. |
| `input_transcript_delta` | Fragmento reconocido del micrófono. |
| `output_transcript_delta` | Fragmento transcrito del audio del asistente. |
| `audio_interrupted` | Vacía inmediatamente el buffer de reproducción. |
| `activity_started` / `activity_ended` | Expone actividad de voz neutral al proveedor. |
| `reconnecting` / `reconnected` | Informa recuperación transparente de la sesión STS. |
| `tool_started` / `tool_completed` | Estado de una tool neutral ejecutada durante la voz. |
| `turn_completed` | Cierra un turno VAD, pero mantiene la sesión abierta. |
| `error` | Error seguro de control, límites o sesión. |

Los bytes de micrófono y reproducción son efímeros y pasan por una cola acotada con
backpressure y un único escritor: no se guardan en PostgreSQL, Redis ni el outbox. Al
completar un turno se persisten únicamente las transcripciones, tool calls y resultados.
Los adaptadores con recuperación transparente reenvían solo comandos no confirmados.
Si el socket se cierra después de haber expuesto salida de un turno proactivo, el servidor
intenta consumir su terminal dentro del presupuesto original del turno para persistirlo
antes de confirmar el comando. Si el proveedor no lo completa a tiempo, el comando se
reencola sin guardar una respuesta parcial.

## Endpoints

| Método | Ruta | Descripción |
| --- | --- | --- |
| `GET` | `/health` | Liveness check sin consultar dependencias externas. |
| `POST` | `/v1/sessions` | Crea una sesión vacía y devuelve su `session_uid`. |
| `GET` | `/v1/sessions` | Lista de forma paginada las sesiones con mensajes pertenecientes al `user_id`. |
| `GET` | `/v1/sessions/{session_uid}/history` | Lee el historial técnico canónico con mensajes, tool calls, resultados y metadatos de orden. |
| `GET` | `/v1/sessions/{session_uid}/group` | Agrupa la conversación principal y sus sesiones internas, threads y jobs sin unir historiales. |
| `WS` | `/v1/agent/ws` | Acceso durable por turnos a la doble capa mediante frames JSON. |
| `WS` | `/v1/agent/realtime` | Primera capa STS full-duplex y el mismo worker de trabajo pesado. |
| `POST` | `/v1/agent/stream` | Acceso SSE por turnos al mismo núcleo de dos agentes. |
| `DELETE` | `/v1/conversations/{conversation_id}` | Borra una conversación del propietario indicado. |

El endpoint de borrado recibe `user_id` como query param:

```bash
curl -X DELETE \
  'http://127.0.0.1:8000/v1/conversations/conv-123?user_id=user-456'
```

El historial técnico también valida `user_id` y pagina por la secuencia canónica. Cada
elemento incluye `turn_id`, `sequence`, `created_at` y un `payload` discriminado como
`message`, `tool_call` o `tool_result`:

```bash
curl \
  'http://127.0.0.1:8000/v1/sessions?user_id=user-456&offset=0&limit=50'

curl \
  'http://127.0.0.1:8000/v1/sessions/0fda5792-2577-4f26-a56d-71f8dd89ac90/history?user_id=user-456&after_sequence=0&limit=50'

curl \
  'http://127.0.0.1:8000/v1/sessions/0fda5792-2577-4f26-a56d-71f8dd89ac90/group?user_id=user-456'
```

## Tools incluidas

| Tool | Capacidad |
| --- | --- |
| `weekly_balance_history` | Espera 2 segundos y devuelve ocho semanas de saldos mock en €. |
| `send_mock_bizum_to_mom` | Simula un Bizum en € al destinatario fijo `Mamá`. |
| `recent_transactions` | Espera 5 segundos y devuelve los diez últimos movimientos categorizados en €. |

`weekly_balance_history` solo está registrada en el worker. Para probar el recorrido
completo de la doble capa, pide al agente interactivo «devuelve mi historial de saldo
por semana». El primer turno responde que va a consultarlo; cuando el job termina, el
agente interactivo recibe el resultado y responde proactivamente por
`/v1/agent/ws`. En una sesión STS, el estado puede consultarse mediante las tools A2A,
pero el resultado todavía no se reinyecta automáticamente para que Gemini lo pronuncie.

`send_mock_bizum_to_mom` también pertenece exclusivamente al worker. Exige un importe
positivo, no permite cambiar el destinatario y devuelve un justificante sintético con
`mock: true`; nunca contacta con un proveedor de pagos ni mueve dinero real.

`recent_transactions` está registrada exclusivamente en el worker, no necesita
argumentos y devuelve un ahorro base, el ahorro actual y los movimientos del más
reciente al más antiguo. Cada movimiento incluye fecha, tipo (ingreso o gasto),
comercio, categoría, cantidad positiva y saldo resultante.

### Evaluación previa de tool calls

El agente interactivo y el worker pueden evaluar cada lote propuesto antes de ejecutar
ninguna tool. El evaluador recibe contratos neutrales: historial acotado, catálogo de tools
y lote propuesto. Devuelve un veredicto estructurado (`pass`, `fail` o `uncertain`), riesgo,
código de motivo y feedback. La rúbrica interactiva comprueba además la elección entre
`delegate_to_worker_agent` y `continue_worker_agent`, la correlación de threads y que el
mensaje delegado sea fiel y autocontenido.

- `off`: no invoca el evaluador.
- `shadow`: registra la evaluación y su consumo, pero ejecuta el lote igualmente.
- `enforce`: solo ejecuta un lote con veredicto `pass`. Los demás se devuelven al agente
  como resultados rechazados para que genere un lote nuevo.

La evaluación es todo-o-nada: si falla una call, no se ejecuta ninguna call del lote. Tras
agotar el límite de revisiones del rol, el turno falla sin ejecutar el último lote. Los
fallos técnicos obedecen a la opción `FAIL_OPEN` correspondiente; para tools con efectos
reales conviene usar `false`. El modo inicial recomendado es `shadow` hasta calibrar un
conjunto representativo de casos. La misma política interactiva protege tanto el agente
textual como el realtime.

En realtime, la evaluación se ejecuta en una tarea separada para no detener la entrega de
audio o texto que el modelo ya esté produciendo. Antes de una tool interactiva, el prompt
exige una única confirmación neutral que no afirme que la operación ya comenzó. La tool no
se ejecuta hasta recibir `pass`.

Los rechazos semánticos usan `disposition="revise_silently"`: el agente corrige la call sin
verbalizar el error. Un fallo técnico del evaluador o el agotamiento de revisiones usa
`disposition="inform_user"`: se devuelve un resultado tipado al modelo para que explique un
fallo general sin revelar detalles internos ni volver a llamar tools en ese turno.

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

## Prompts

Los prompts por defecto están versionados como Markdown en:

- `src/prompts/interactive_agent.md`: agente que conversa con el usuario.
- `src/prompts/realtime_agent.md`: reglas añadidas solo al agente STS persistente.
- `src/prompts/worker_agent.md`: agente persistente que ejecuta las tools operativas.
- `src/prompts/interactive_tool_call_evaluator.md`: rúbrica de calls del agente principal.
- `src/prompts/tool_call_evaluator.md`: rúbrica del evaluador de calls del worker.

Las evaluaciones se guardan como trazas de auditoría separadas del historial que se envía a
los modelos. La vista **Historial** las muestra dentro del turno y conversación evaluados con
correlación, intento, calls propuestas, veredicto, riesgo, motivo, feedback redactado, política,
ejecución, modelo, consumo, latencia y fecha. La API las expone en `evaluations` dentro de
`GET /v1/sessions/{session_uid}/history`; `evaluations_truncated` indica si la proyección
alcanzó el límite de auditoría.

`config.py` los carga mediante una ruta relativa al código, independientemente del
directorio desde el que se arranque el proceso. `AGENT_INSTRUCTIONS`,
`REALTIME_AGENT_INSTRUCTIONS` y `WORKER_AGENT_INSTRUCTIONS` pueden sobrescribirlos desde
el entorno sin modificar los archivos versionados.

## Configuración

| Variable | Valor por defecto | Propósito |
| --- | --- | --- |
| `TEXT_AGENT_PROVIDER` | `openai` | Proveedor turn-based de `/v1/agent/ws` y SSE. |
| `TEXT_AGENT_MODEL` | `gpt-5-mini` | Modelo del agente textual. |
| `REALTIME_AGENT_PROVIDER` | `gemini` | Adaptador STS de `/v1/agent/realtime`; admite `gemini` u `openai`. |
| `REALTIME_AGENT_MODEL` | `gemini-3.1-flash-live-preview` | Modelo realtime; con OpenAI usa `gpt-realtime-2.1`. |
| `WORKER_PROVIDER` | `openai` | Proveedor del agente de trabajo pesado. |
| `OPENAI_API_KEY` | — | Credencial de OpenAI. |
| `OPENAI_BASE_URL` | — | Base URL alternativa compatible. |
| `OPENAI_REALTIME_VOICE_NAME` | `marin` | Voz del adaptador OpenAI Realtime. |
| `OPENAI_REALTIME_TRANSCRIPTION_MODEL` | `gpt-4o-mini-transcribe` | Transcripción auxiliar del audio de entrada. |
| `OPENAI_REALTIME_LANGUAGE_CODE` | inferido | Idioma opcional de la transcripción de entrada. |
| `OPENAI_REALTIME_REASONING_EFFORT` | inferido | Esfuerzo de razonamiento opcional del modelo realtime. |
| `WORKER_AGENT_MODEL` | `gpt-5-mini` | Modelo del agente de trabajo. |
| `EVALUATION_PROVIDER` | `openai` | Adaptador del evaluador de tool calls. |
| `EVALUATION_MODEL` | `gpt-5.4-mini` | Modelo pequeño usado por el evaluador. |
| `EVALUATION_REASONING_EFFORT` | `none` | Esfuerzo de razonamiento del evaluador OpenAI. |
| `INTERACTIVE_TOOL_EVALUATION_MODE` | `off` | Evaluador del agente textual y realtime: `off`, `shadow` o `enforce`. |
| `INTERACTIVE_TOOL_EVALUATION_TIMEOUT_SECONDS` | `15` | Presupuesto de cada evaluación interactiva. |
| `INTERACTIVE_TOOL_EVALUATION_MAX_REVISIONS` | `2` | Lotes rechazados que el agente principal puede revisar. |
| `INTERACTIVE_TOOL_EVALUATION_FAIL_OPEN` | `false` | Devuelve un fallo general al agente si el evaluador interactivo falla; `true` ejecuta el lote. |
| `WORKER_TOOL_EVALUATION_MODE` | `off` | Control del evaluador: `off`, `shadow` o `enforce`. |
| `WORKER_TOOL_EVALUATION_TIMEOUT_SECONDS` | `15` | Presupuesto de cada evaluación. |
| `WORKER_TOOL_EVALUATION_MAX_REVISIONS` | `2` | Lotes rechazados que el agente puede revisar. |
| `WORKER_TOOL_EVALUATION_FAIL_OPEN` | `true` | Ejecuta el lote si el evaluador sufre un fallo técnico. |
| `MODEL_PRICING` | catálogo de los modelos predeterminados | JSON de tarifas por millón de tokens (`input`, `cached_input`, `output`, `input_audio`, `cached_input_audio`, `output_audio` y `currency`), indexado por nombre de modelo. |
| `OPENAI_CONNECT_TIMEOUT_SECONDS` | `15` | Timeout de conexión. |
| `GEMINI_API_KEY` | — | Credencial del adaptador Gemini realtime disponible inicialmente. |
| `GEMINI_LIVE_API_VERSION` | `v1beta` | Versión de la API Live usada por el SDK. |
| `GEMINI_LIVE_VOICE_NAME` | `Zephyr` | Voz predefinida de salida. |
| `GEMINI_LIVE_LANGUAGE_CODE` | inferido | Idioma opcional de la voz. |
| `REALTIME_AUDIO_MAX_CHUNK_BYTES` | `32768` | Máximo por frame PCM16 de entrada. |
| `REALTIME_SESSION_MAX_SECONDS` | `1800` | Duración máxima de una conexión STS. |
| `REALTIME_OUTBOUND_MAX_MESSAGES` | `128` | Comandos pendientes máximos del escritor único. |
| `REALTIME_OUTBOUND_MAX_AUDIO_BYTES` | `131072` | Audio PCM máximo pendiente por sesión. |
| `REALTIME_OUTBOUND_ENQUEUE_TIMEOUT_SECONDS` | `5` | Presupuesto de backpressure antes de cerrar. |
| `REALTIME_RESUMPTION_MAX_ATTEMPTS` | `3` | Intentos máximos de recuperación transparente. |
| `REALTIME_RESUMPTION_TIMEOUT_SECONDS` | `15` | Presupuesto por intento de recuperación. |
| `REALTIME_PROACTIVE_TURN_TIMEOUT_SECONDS` | `120` | Tiempo máximo para anunciar un resultado A2A. |
| `REALTIME_COMMAND_RECONCILIATION_SECONDS` | `5` | Recuperación de notificaciones realtime perdidas. |
| `POSTGRES_URL` | `postgresql://.../tesseraflow` | Fuente canónica de conversaciones. |
| `POSTGRES_POOL_MIN_SIZE` | `1` | Conexiones mínimas por proceso. |
| `POSTGRES_POOL_MAX_SIZE` | `10` | Conexiones máximas por proceso. |
| `POSTGRES_COMMAND_TIMEOUT_SECONDS` | `30` | Timeout de comandos SQL. |
| `REDIS_URL` | `redis://localhost:6379/0` | Caché de contexto reciente. |
| `MAX_TOOL_ROUNDS` | `8` | Límite contra bucles de tools. |
| `A2A_WORKER_RECONCILIATION_SECONDS` | `5` | Respaldo periódico para señales A2A perdidas y leases vencidas. |
| `A2A_JOB_TIMEOUT_SECONDS` | `600` | Tiempo máximo de un turno del worker. |
| `INTERACTION_COORDINATOR_RECONCILIATION_SECONDS` | `5` | Reconciliación de comandos si no llega una notificación. |
| `INTERACTION_OUTPUT_RECONCILIATION_SECONDS` | `5` | Reconciliación de outputs si no llega una notificación. |
| `INTERACTION_COMMAND_TIMEOUT_SECONDS` | `120` | Tiempo máximo de un turno interactivo. |
| `INTERACTION_MAX_PENDING_COMMANDS` | `16` | Entradas de usuario pendientes permitidas por conversación. |
| `INTERACTION_COORDINATOR_WORKERS` | `4` | Turnos máximos concurrentes por proceso; una conversación sigue serializada. |
| `CONVERSATION_TTL_SECONDS` | `604800` | TTL de la caché; no borra PostgreSQL. |
| `CONVERSATION_MAX_MESSAGES` | `100` | Máximo de elementos en el contexto reciente. |
| `CONVERSATION_MAX_CHARACTERS` | `200000` | Límite lógico del contexto reciente. |
| `CONVERSATION_MAX_BYTES` | `512000` | Límite del JSON guardado en Redis. |
| `LOG_LEVEL` | `INFO` | Nivel de logging. |
| `LOG_JSON` | `false` | Activa logs JSON estructurados. |

Los endpoints textual y realtime se configuran simultáneamente:

```dotenv
TEXT_AGENT_PROVIDER=openai
TEXT_AGENT_MODEL=gpt-5-mini
REALTIME_AGENT_PROVIDER=gemini
REALTIME_AGENT_MODEL=gemini-3.1-flash-live-preview
WORKER_PROVIDER=openai
WORKER_AGENT_MODEL=gpt-5
GEMINI_API_KEY=...
OPENAI_API_KEY=...
REALTIME_AUDIO_MAX_CHUNK_BYTES=32768
REALTIME_SESSION_MAX_SECONDS=1800
MODEL_PRICING='{"mi-modelo":{"input":0.44,"cached_input":0.09,"output":1.76,"currency":"€"}}'
```

Cada gateway traduce el uso de su SDK a contadores comunes. El núcleo aplica este
catálogo, agrega las llamadas intermedias de tools y persiste uso y coste con la
respuesta del turno. Si un modelo no aparece en `MODEL_PRICING`, se conservan sus
tokens y el coste se expone como no configurado, evitando estimaciones silenciosas.

### Referencia de precios y moneda

El catálogo incluido fue revisado el **29-07-2026** y expresa tarifas por millón
de tokens en €:

- `gpt-5.4-mini`: `0.66` € entrada, `0.07` € entrada cacheada y `3.96` € salida.
- `gpt-5.4`: tarifa retail de Azure OpenAI Global Standard en Sweden Central:
  `2.193945` € entrada, `0.219394` € entrada cacheada y `13.163668` € salida.
  Por encima de 272.000 tokens de entrada se activa automáticamente el tier de
  contexto largo: `4.387889`, `0.438789` y `19.745502` € respectivamente.
- `gemini-3.1-flash-live-preview`: precios oficiales de Google en USD convertidos
  con la referencia BCE del 29-07-2026 (`1 € = 1.1380 USD`): `0.659051` €
  entrada de texto, `2.636204` € entrada de audio, `3.954306` € salida de
  texto/pensamiento y `10.544815` € salida de audio.

Estas cifras calculan consumo técnico antes de IVA, descuentos contractuales o
el cambio aplicado en la factura. Si el deployment de Azure es Data Zone en vez
de Global Standard, sustituye el bloque `gpt-5.4` por las tarifas Data Zone del
recurso; el endpoint por sí solo no permite descubrir el tipo de deployment.

### Cómo se cuentan los tokens realtime

El contador no estima tokens desde el audio ni desde las transcripciones. El
gateway usa exclusivamente las métricas que envía el proveedor en cada respuesta:

- Gemini Live entrega periódicamente `usageMetadata`. Dentro de un mismo
  `model turn` esos mensajes son snapshots acumulativos, no deltas: el gateway
  conserva únicamente el último snapshot y lo contabiliza una sola vez cuando
  recibe `turnComplete=true`. `promptTokenCount` es toda la entrada que
  el modelo procesó para esa petición de generación, incluido el contexto anterior;
  `promptTokensDetails` separa modalidades como texto y audio.
  `responseTokenCount` es la salida, `thoughtsTokenCount` el razonamiento facturable
  y `toolUsePromptTokenCount` la entrada adicional causada por resultados de tools.
  `totalTokenCount` describe esa petición (`prompt` más las demás categorías
  facturables), no el acumulado histórico de toda la conexión. El total de sesión
  se obtiene sumando los snapshots finales de cada `model turn`.
- `cachedContentTokenCount` es un subconjunto de `promptTokenCount`, no una cantidad
  adicional. El coste de entrada nueva/no cacheada se calcula como
  `input_tokens - cached_input_tokens`. El modelo realtime configurado actualmente,
  `gemini-3.1-flash-live-preview`, no soporta caché, por lo que todo el contexto
  procesado vuelve a facturarse como entrada en cada turno.
- OpenAI Realtime entrega el objeto `usage` dentro de `response.done`, con
  `input_tokens`, `output_tokens`, detalles de texto/audio y `cached_tokens`.
  Los tokens cacheados también son un subconjunto de la entrada total.

El proveedor no informa cuántos tokens no cacheados pertenecen exclusivamente al
mensaje nuevo frente al historial previo. Sí informa el total facturable del prompt
y su parte cacheada, que es lo necesario para calcular el coste real del turno.

Las combinaciones sin adaptador registrado fallan durante la composición, antes de
aceptar tráfico. Incorporar otro proveedor solo requiere añadir su gateway y registrarlo
en `infrastructure/model_runtime.py`; `bootstrap.py` y el núcleo no cambian.

Para seleccionar OpenAI STS:

```dotenv
REALTIME_AGENT_PROVIDER=openai
REALTIME_AGENT_MODEL=gpt-realtime-2.1
OPENAI_REALTIME_VOICE_NAME=marin
```

Consulta [.env.example](.env.example) para ver una configuración completa.

## Ciclos de vida y concurrencia

- Los clientes de modelos seleccionados, el pool PostgreSQL y Redis se crean una vez por
  proceso durante el `lifespan` y se cierran durante el apagado.
- Cada turno crea una `ModelSession` ligera; STS crea una `RealtimeModelSession` por
  WebSocket y la cierra al desconectar o alcanzar su tiempo máximo.
- No se guarda usuario, historial ni `response_id` en servicios compartidos.
- El worker se inicia y detiene con el `lifespan`; una interrupción libera su job.
- El coordinador y su conexión `LISTEN` se inician con el `lifespan`; una interrupción
  libera sus comandos y cierra el listener.
- Los jobs sobreviven a reinicios y una lease vencida permite reclamarlos de nuevo.
- Dos jobs del mismo thread nunca se ejecutan a la vez.
- Dos comandos de la misma conversación nunca invocan a la vez al modelo interactivo.
- Conversaciones distintas pueden ocupar hasta `INTERACTION_COORDINATOR_WORKERS`
  ejecuciones simultáneas por proceso.
- Un cierre del WebSocket detiene la entrega, no el comando durable ya aceptado.
- Un outbox sin confirmar permite reanudar la entrega tras una reconexión.
- Las tools operativas solo se exponen al worker. Si solicita varias, actualmente se
  ejecutan concurrentemente y sus resultados se devuelven juntos.
- Las cancelaciones se propagan y los streams se liberan mediante context managers.

`InteractionCommand.source` distingue `text_user`, `speech_user` y `worker_agent`. El
camino durable por turnos usa inbox/outbox; el camino STS prioriza baja latencia y comunica
media directamente. Ambos convergen en historial textual neutral, tools A2A y control de
propiedad. Los resultados proactivos del worker continúan siendo durables en
`/v1/agent/ws`; desde STS también pueden consultarse mediante las tools A2A.

## Seguridad y límites actuales

- Los logs no incluyen mensajes, argumentos, resultados, claves ni datos sensibles.
- Los errores esperables de una tool se convierten en resultados estructurados.
- Los historiales no se resumen automáticamente: se eliminan turnos antiguos completos
  para evitar alterar información sensible.
- Mensajes, argumentos y resultados sí forman parte del historial persistido. El
  cifrado, la clasificación de datos y la retención legal dependen del despliegue.
- El audio realtime no se persiste, pero sus transcripciones sí forman parte del historial.
- La autenticación, la autorización externa, el cifrado a nivel de aplicación y las
  políticas regulatorias específicas quedan fuera del alcance de esta base.

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
├── prompts/             # Instrucciones Markdown de cada capa de agente
├── tools/               # Tools concretas y registro
├── bootstrap.py         # Composición de dependencias
└── main.py              # Aplicación y lifespan
tests/                   # Tests unitarios de casos de uso y adaptadores
frontend/                # Cliente React + TypeScript para texto y voz realtime
```

## Roadmap

Las capacidades futuras y las decisiones pendientes se mantienen en
[ROADMAP.md](ROADMAP.md). Esto evita presentar como disponible una funcionalidad que
todavía no está implementada.
