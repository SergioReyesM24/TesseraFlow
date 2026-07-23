# Preguntas frecuentes

Este documento responde preguntas habituales sobre la arquitectura y el comportamiento
actual de TesseraFlow. Se ampliará conforme aparezcan nuevas dudas sobre los flujos de
texto, realtime y los jobs entre agentes.

## Arquitectura general

### ¿TesseraFlow utiliza un único agente?

TesseraFlow separa tres roles:

1. Un agente interactivo turn-based para `/v1/agent/ws` y `/v1/agent/stream`.
2. Un agente interactivo speech-to-speech persistente para `/v1/agent/realtime`.
3. Un worker compartido que ejecuta trabajos pesados y tools operativas.

Texto y realtime son dos motores de interacción para una misma identidad
conversacional. Comparten contratos de dominio, conversación, tools A2A, repositorios y
worker, pero pueden usar proveedores, modelos y definiciones diferentes.

El worker sí es un agente separado: mantiene su propia conversación y solo se comunica
con el agente interactivo mediante jobs A2A.

### ¿Texto y realtime son dos asistentes construidos completamente por separado?

No. Comparten las piezas estables del sistema:

- `ConversationRepository` y el historial canónico.
- `AgentDefinition`, `AgentResult`, mensajes, tool calls y tool results.
- `ToolRegistry`, validación de argumentos y `ToolExecutor`.
- Las tools `delegate_to_worker_agent`, `continue_worker_agent` y
  `get_worker_agent_status`.
- `A2AService`, la cola de jobs, el worker y sus conversaciones.
- PostgreSQL, Redis y las reglas de propiedad por conversación y usuario.

La orquestación interactiva sí es diferente. Texto abre una sesión de modelo acotada por
turno; realtime mantiene una conexión full-duplex durante todo el WebSocket. Unificar
ambas dentro de una única sesión añadiría estados y condicionales que no existen en el
otro flujo.

### ¿Qué partes son específicas de realtime?

Realtime añade responsabilidades que no necesita el flujo textual:

- Audio bidireccional.
- VAD automático o actividad explícita.
- Barge-in e interrupción del audio generado.
- Una conexión persistente con el proveedor.
- Una cola outbound acotada y un escritor único.
- Backpressure por número de mensajes y bytes de audio.
- Recuperación o reanudación de la conexión.
- Inyección de finalizaciones A2A en una sesión STS activa.

Estas responsabilidades viven detrás de contratos neutrales al proveedor. Gemini es el
adaptador realtime disponible inicialmente, no una dependencia del dominio o de la API.

## Jobs y worker

### ¿Qué sucede cuando el agente llama a `delegate_to_worker_agent`?

La tool crea un thread A2A y un job durable en PostgreSQL. El job contiene, entre otros
datos:

- El identificador del thread.
- El identificador del job.
- La conversación principal que lo originó.
- La conversación interna del worker.
- El mensaje de trabajo.
- El modo de entrega: `turn_based` o `realtime`.

La tool devuelve inmediatamente un recibo con `thread_id`, `job_id` y estado `queued`.
No espera a que el worker termine.

### ¿El worker es diferente según el endpoint de origen?

No. Los jobs creados desde texto y realtime entran en la misma cola y son procesados por
el mismo `A2AWorker`, con el mismo modelo, definición y catálogo de tools operativas.

La diferencia solo aparece al entregar el resultado al agente interactivo. Cada job
conserva el `delivery_mode` del turno que lo creó.

### ¿Cómo se entrega un job creado desde `/v1/agent/ws`?

El job usa `delivery_mode="turn_based"`. Al terminar el worker:

1. PostgreSQL actualiza el job y crea un comando `worker_completed` atómicamente.
2. El `ConversationCoordinator` reclama el comando.
3. Se abre una nueva sesión turn-based del modelo interactivo.
4. El modelo recibe el resultado estructurado del worker.
5. La respuesta generada se persiste y sus eventos se escriben en el outbox.
6. El WebSocket entrega los eventos pendientes cuando está conectado.

El trabajo y la respuesta proactiva no dependen de que siga abierto el socket que creó
el job. Si no hay un cliente conectado, los outputs permanecen durables hasta una
conexión posterior.

### ¿Cómo se entrega un job creado desde `/v1/agent/realtime`?

El job usa `delivery_mode="realtime"`. Al terminar el worker:

1. PostgreSQL actualiza el job y crea un comando `worker_completed` con el mismo modo.
2. El coordinador turn-based no puede reclamarlo.
3. Una sesión realtime de la conversación espera a estar sin turno activo y con el
   micrófono pausado.
4. La sesión reclama el comando y envía el resultado A2A al proveedor STS mediante
   `send_text`.
5. El proveedor genera la respuesta hablada y su transcripción.
6. El turno `worker_agent → assistant` se persiste.
7. El comando solo se confirma después del evento terminal real del proveedor.

Por tanto, la respuesta proactiva la produce el modelo realtime configurado dentro de la
conexión activa. No se utiliza el agente textual para redactarla.

### ¿Qué ocurre si termina un job realtime y no hay ningún socket conectado?

El comando permanece pendiente en la inbox durable de PostgreSQL. No se llama a ningún
modelo y no se hace fallback al agente turn-based.

Cuando vuelva a abrirse una sesión realtime para la misma conversación y usuario, la
sesión podrá reclamar el comando e inyectarlo en el proveedor STS.

### ¿Un job realtime puede activar accidentalmente el agente textual?

No en el comportamiento actual. Los consumidores están separados por `delivery_mode`:

- El coordinador global reclama únicamente `turn_based`.
- Las sesiones realtime reclaman únicamente `realtime` para su conversación y usuario.

Si un despliegue actualizado muestra una respuesta textual inesperada para un job
realtime, debe comprobarse que la migración
`007_interaction_delivery_modes.sql` se haya aplicado y que todos los procesos ejecuten
la misma versión.

### ¿Qué pasa si la sesión realtime se desconecta mientras responde a un job?

Si el comando todavía no ha sido confirmado, se devuelve a la inbox. La entrega es al
menos una vez durante una caída previa a la confirmación.

Los identificadores `job_id`, `causation_id` y los checkpoints del proveedor limitan el
riesgo de duplicados, pero un consumidor debe asumir que existe una ventana estrecha en
la que puede repetirse una entrega después de una caída.

## Historial de conversación

### ¿Realtime recibe el historial de conversación?

Sí. Al abrir `/v1/agent/realtime` se carga el historial persistido de la conversación y
se entrega al `RealtimeModelGateway`. El adaptador lo traduce al formato del proveedor y
lo envía como contenido inicial de la sesión.

La diferencia con una ejecución turn-based es cuándo se envía:

```text
Texto:
cada comando → cargar historial → abrir ModelSession → enviar turno

Realtime:
abrir WebSocket → cargar historial una vez → mantener RealtimeModelSession
```

Los turnos posteriores no necesitan reenviar todo el historial porque el proveedor ya
mantiene el contexto de la conexión persistente.

### ¿Los turnos realtime solo se persisten o también forman parte del contexto vivo?

Forman parte de ambos:

- El proveedor los conoce porque ocurrieron dentro de su sesión persistente.
- Al llegar el evento terminal, las transcripciones, tool calls, tool results y respuesta
  se guardan en el historial canónico.

El audio crudo de entrada y salida es efímero y no se almacena en PostgreSQL ni Redis.

### ¿Qué ocurre al abrir una nueva conexión realtime?

La nueva conexión carga el historial persistido más reciente y lo vuelve a presentar al
proveedor seleccionado. No depende del estado efímero de la conexión anterior.

En una recuperación transparente de la misma conexión se utiliza el mecanismo de
reanudación del proveedor y se reenvían únicamente comandos enviados pero no
confirmados; no se reconstruye todo el historial desde cero.

### ¿Realtime ve mensajes añadidos por otro canal mientras su conexión sigue abierta?

No automáticamente. Realtime carga el historial al abrir la sesión. Si después otro
canal añade turnos a la misma conversación, esos turnos quedan persistidos, pero no se
inyectan en la conexión STS ya abierta.

Por este motivo no se recomienda utilizar simultáneamente texto y realtime sobre la
misma conversación. Una futura coordinación global por conversación podría convertir
esta restricción en un comportamiento explícitamente soportado.

### ¿Puede aparecer `ConversationConflictError`?

El enrutamiento por `delivery_mode` evita el conflicto conocido en el que una
finalización realtime activaba simultáneamente al agente textual. Sin embargo, todavía
puede existir un conflicto si la misma conversación recibe turnos intencionadamente
concurrentes desde texto, varias sesiones realtime o varios procesos.

La estrategia segura actual es mantener un único canal interactivo activo por
conversación.

## Colas y entrega realtime

### ¿Existe una cola común para enviar audio, texto, tools y jobs al proveedor realtime?

Sí. Cada `RealtimeAgentSession` de producción tiene una cola outbound acotada y una única
coroutine escritora.

Por ella pasan:

- Fragmentos de audio.
- Fin del stream de audio.
- Inicio y fin de actividad explícita.
- Mensajes textuales realtime.
- Resultados de tools.
- Finalizaciones A2A.
- Cierre del escritor.

WebSocket, tools y dispatcher A2A no escriben directamente en el SDK del proveedor. La
coroutine escritora conserva el orden FIFO y evita llamadas de escritura concurrentes.

### ¿La cola outbound realtime es durable?

No. Es una cola acotada en memoria y pertenece a una conexión concreta. Su objetivo es
ordenar las escrituras, aplicar backpressure y limitar memoria, no sobrevivir a la caída
del proceso.

Las finalizaciones A2A tienen un nivel durable anterior:

```text
interaction_commands en PostgreSQL
              ↓
cola outbound de la sesión realtime
              ↓
escritor único
              ↓
proveedor STS
```

### ¿Qué entradas realtime son durables antes de enviarse al proveedor?

| Entrada | Cola outbound local | Durable antes del envío |
| --- | ---: | ---: |
| Audio del usuario | Sí | No |
| Texto enviado dentro de realtime | Sí | No |
| Inicio o fin de actividad | Sí | No |
| Resultado de una tool | Sí | No; queda en el turno al completarse |
| Finalización de un worker | Sí | Sí |

Los mensajes normales de `/v1/agent/ws` no usan esta cola. Se guardan primero en la
inbox turn-based de PostgreSQL.

### ¿Qué sucede si se satura la cola realtime?

La aplicación aplica backpressure sin descartar silenciosamente audio. Si se supera el
tiempo máximo configurado para encolar, la sesión devuelve el error
`realtime_backpressure_exceeded` y cierra el WebSocket.

Los límites principales son:

- `REALTIME_OUTBOUND_MAX_MESSAGES`
- `REALTIME_OUTBOUND_MAX_AUDIO_BYTES`
- `REALTIME_OUTBOUND_ENQUEUE_TIMEOUT_SECONDS`

## Proveedores y configuración

### ¿Realtime está ligado a Gemini?

No en el núcleo. Aplicación, dominio y API dependen de `RealtimeModelGateway`,
`RealtimeModelSession` y eventos neutrales.

Gemini es el adaptador registrado actualmente. Añadir otro proveedor requiere una nueva
implementación de infraestructura, su registro en la composición y tests de traducción,
sin modificar la lógica A2A ni la API pública.

### ¿Texto y realtime pueden utilizar proveedores o modelos diferentes?

Sí. Se configuran independientemente mediante:

- `TEXT_AGENT_PROVIDER`
- `TEXT_AGENT_MODEL`
- `REALTIME_AGENT_PROVIDER`
- `REALTIME_AGENT_MODEL`
- `WORKER_PROVIDER`
- `WORKER_AGENT_MODEL`

Las combinaciones para las que no exista un adaptador registrado fallan durante el
arranque.

## Durabilidad resumida

| Comportamiento | Texto por WebSocket | Realtime STS |
| --- | --- | --- |
| Entrada normal del usuario | Inbox durable | Directa a la sesión |
| Ejecución independiente del socket | Sí | No |
| Job del worker | Durable | Durable |
| Finalización del worker | Durable | Durable |
| Respuesta proactiva sin socket | Se genera y queda en outbox | Espera sin generarse |
| Salida del modelo | Outbox durable | Entrega en vivo |
| Audio crudo persistido | No | No |
| Historial semántico terminal | Sí | Sí |

## Más información

- [README](README.md)
- [Roadmap](ROADMAP.md)
- [Configuración de ejemplo](.env.example)
