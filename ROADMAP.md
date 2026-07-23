# Roadmap técnico

Este documento recoge funcionalidades deliberadamente fuera del alcance de la fase
actual. Su implementación deberá conservar los contratos neutrales al proveedor, el
aislamiento entre usuarios y la separación entre dominio, aplicación y adaptadores.

## Evolución de componentes visuales

El catálogo v1 actual ya publica eventos neutrales `visual_component`, valida gráficas
`line`/`bar` y grupos de métricas, conserva un fallback textual y funciona en los
transportes WebSocket y SSE. Las siguientes extensiones continúan fuera de alcance:

- Añadir tablas y avisos solo cuando exista un caso de producto que justifique cada
  semántica, manteniendo límites de filas, columnas y contenido.
- Diseñar formularios de confirmación como un protocolo de entrada independiente con
  autorización, expiración, idempotencia y protección frente a dobles envíos.
- Persistir componentes como elementos canónicos del historial si el frontend debe
  reconstruirlos después de que el evento del outbox haya sido entregado.
- Negociar capacidades del cliente cuando sea necesario omitir por completo eventos que
  no comprende, sin debilitar el fallback textual obligatorio.
- Incorporar nuevos tipos de gráfica únicamente mediante una nueva variante validada o
  versión del esquema; no aceptar configuraciones opacas de librerías de frontend.

## Evolución de la entrega proactiva

La inbox serializada por conversación, los comandos de finalización A2A y el outbox
durable ya forman parte de la arquitectura actual. Las siguientes extensiones continúan
fuera de alcance:

- Añadir prioridades, resultados parciales, cancelación pública y límites de consumo por
  usuario a los contratos de jobs existentes.
- Separar opcionalmente el coordinador en un proceso desplegable de forma independiente;
  PostgreSQL ya actúa como frontera durable compartida.
- Permitir que ambas capas trabajen a la vez y publiquen resultados parciales mediante
  eventos neutrales, conservando la trazabilidad con `request_id`, `conversation_id` y
  `job_id`.
- Definir cómo un resultado de segundo plano complementa, corrige o sustituye una
  respuesta previa sin producir actualizaciones incoherentes ni duplicadas.
- Versionar los resultados y aplicar control de concurrencia para descartar entregas
  tardías u obsoletas.
- Añadir fan-out a varios sockets de la misma conversación y consumidores durables
  independientes; la entrega actual confirma cada evento una sola vez.
- Añadir webhooks o polling de outputs para consumidores sin WebSocket, sin convertir
  ningún transporte en fuente de verdad.
- Propagar cancelaciones cuando sea posible y definir políticas explícitas para tareas
  que deban continuar tras la desconexión del cliente.
- Añadir observabilidad de tiempos en cola, latencia de la primera respuesta, duración
  del razonamiento, uso de recursos, errores y resultados descartados, sin registrar
  datos personales ni contenido sensible.
- Probar el aislamiento entre ejecuciones concurrentes, la recuperación tras reinicios,
  la cancelación, los timeouts, los resultados fuera de orden y los fallos parciales de
  cada capa.

## Escalado independiente de la cola A2A

La cola A2A actual ya usa PostgreSQL como fuente de verdad, `LISTEN/NOTIFY` como señal de
baja latencia y reconciliación periódica para recuperar notificaciones perdidas y leases
vencidas. Existe un `A2AWorker` por proceso FastAPI, por lo que el número de consumidores
A2A crece actualmente con las réplicas web y cada `NOTIFY` despierta a todos esos
consumidores. No se crea un listener por usuario o conversación, pero a gran escala el
broadcast entre procesos podría producir demasiados intentos fallidos de `claim_next()`.

La siguiente evolución deberá desacoplar la capacidad web de la capacidad de trabajo A2A:

- Crear puntos de entrada o roles de despliegue explícitos para API y worker, de modo que
  los procesos FastAPI puedan limitarse a aceptar tráfico y persistir jobs, mientras uno o
  varios procesos A2A independientes los consumen.
- Mantener PostgreSQL y `A2AJobRepository.claim_next()` como autoridad de orden, leases y
  ownership. El payload de `NOTIFY` seguirá siendo únicamente una señal para reconciliar
  estado durable, nunca una orden de ejecución confiable.
- Crear un `A2AWorkerPool` con una sola suscripción `LISTEN` por proceso, una capacidad
  concurrente explícita y acotada, y un bucle que reclame jobs hasta llenar sus slots
  disponibles. No crear una suscripción independiente por coroutine ejecutora.
- Permitir configurar por separado el número de réplicas worker y la concurrencia local,
  por ejemplo mediante `A2A_WORKER_CONCURRENCY`, sin vincularlos al número de workers de
  Uvicorn ni al número de usuarios conectados.
- Ejecutar simultáneamente jobs de threads distintos, conservando la serialización actual
  dentro de cada thread mediante la consulta durable. La concurrencia local no deberá
  introducir un segundo mecanismo de orden en memoria.
- Coordinar notificaciones, slots liberados, reconciliación y apagado mediante primitivas
  acotadas. El cierre deberá dejar de reclamar trabajo, propagar cancelaciones y reencolar
  de forma segura las claims interrumpidas sin cerrar clientes compartidos prematuramente.
- Separar el listener A2A del listener usado por los procesos API cuando se desplieguen
  roles distintos, evitando que una réplica web consulte `a2a_jobs` si no ejecutará jobs.
- Añadir reconexión con backoff y jitter a la conexión PostgreSQL `LISTEN`. Mientras se
  recupera la conexión, la reconciliación periódica deberá preservar la corrección aunque
  aumente temporalmente la latencia.

Observabilidad necesaria antes de aumentar consumidores:

- Medir profundidad de la cola y tiempo `created_at` a `started_at` por job y thread.
- Contabilizar intentos de `claim_next()` exitosos y vacíos, distinguiendo despertares por
  `NOTIFY`, capacidad liberada y reconciliación periódica.
- Medir jobs activos, utilización del pool, duración, timeouts, leases vencidas y valores
  de `attempt_count` superiores a uno.
- Registrar conexiones y reconexiones del listener, señales recibidas y tiempo hasta el
  primer claim, sin incluir mensajes, argumentos de tools ni resultados.
- Definir umbrales operativos para escalar réplicas o concurrencia sin saturar PostgreSQL,
  proveedores de modelos ni sistemas externos invocados por las tools.

Si el broadcast entre réplicas worker se convierte en un cuello de botella, evaluar en
este orden:

1. Reducir y controlar el número de procesos que escuchan A2A mediante el despliegue
   independiente y el pool local.
2. Particionar opcionalmente por un hash estable de `thread_id`, asignando todos los jobs
   del mismo thread al mismo shard y canales de notificación. Deberán diseñarse
   explícitamente el rebalanceo, la recuperación de workers caídos, los shards calientes
   y el aprovechamiento de capacidad libre.
3. Adoptar un broker con competing consumers —por ejemplo Redis Streams, RabbitMQ, SQS o
   Kafka— solo cuando las métricas demuestren que PostgreSQL ya no satisface el throughput
   o la latencia requeridos.

La introducción de un broker deberá usar un outbox transaccional: la misma transacción que
crea el job persistirá el mensaje pendiente de publicación, un publisher independiente lo
entregará al broker y el consumidor continuará validando de forma idempotente el estado
del job en PostgreSQL. No se publicará directamente al broker después de insertar el job,
porque una caída entre ambas operaciones podría dejar trabajo durable sin señal de
entrega. Esta fase deberá definir duplicados, claves de idempotencia, redelivery,
dead-letter queues, backpressure y reconciliación entre broker y base de datos.

Pruebas previstas:

- Verificar que una sola notificación llena varios slots disponibles sin crear una
  suscripción ni una consulta simultánea por slot.
- Comprobar el límite de concurrencia, la ejecución paralela entre threads y el orden
  estricto dentro de un mismo thread.
- Simular varios procesos competidores, notificaciones duplicadas o perdidas, caída del
  listener, expiración de leases y apagado con claims activas.
- Confirmar que procesos configurados únicamente como API no escuchan ni consultan la cola
  A2A y que las réplicas worker pueden escalar de forma independiente.
- Medir y acotar el número de claims vacíos por job bajo diferentes cantidades de
  procesos, concurrencia y carga sostenida.
- Para un futuro broker, probar publicación outbox idempotente, redelivery, duplicados,
  caída antes y después del ack y recuperación sin perder ni ejecutar dos veces efectos no
  idempotentes.

## Evolución de la interacción STS

El canal STS actual ya dispone de contratos independientes del proveedor, cola acotada,
escritor único, backpressure, actividad automática o explícita, recuperación transparente
cuando el adaptador la soporta y entrega proactiva durable de resultados A2A. Las
extensiones que continúan fuera de alcance son:

- Añadir WebRTC cuando se necesiten jitter buffers, negociación de codecs, cancelación de
  eco y transporte adaptativo frente a WebSocket PCM.
- Definir políticas desplegables de consentimiento, retención de transcripciones y borrado
  de datos de voz, aunque el audio crudo actual no se almacene.
- Añadir métricas de latencia hasta el primer audio, interrupciones, duración de sesión,
  bytes descartados y conflictos de persistencia.
- Probar navegadores y dispositivos reales con cancelación de eco, pérdida de red,
  reconexión y cambios de dispositivo de entrada.

## Contabilidad de uso y costes de modelos

Persistir el consumo y el coste estimado de los modelos por turno, tanto para ejecuciones
de texto como para sesiones STS, sin depender de los formatos de facturación de un
proveedor concreto.

Aspectos que deberá cubrir:

- Definir un registro neutral asociado al turno con proveedor, modelo, rol del agente,
  modalidad, `conversation_id`, `response_id`, unidades consumidas y coste estimado.
- Traducir en cada adaptador las unidades que exponga el proveedor, como tokens de entrada
  y salida, tokens en caché, audio o duración de una sesión STS.
- Guardar los registros en una base de datos durable y permitir agregaciones por turno,
  conversación, modelo, proveedor y periodo temporal.
- Mantener las tarifas fuera del núcleo, versionadas y con fechas de vigencia, para poder
  recalcular costes y distinguir estimaciones de importes facturados por el proveedor.
- No persistir prompts, respuestas, audio ni argumentos de tools como parte de la
  contabilidad de costes.

## Reintentos

Añadir políticas de reintentos diferenciadas para llamadas al modelo, herramientas y
almacenamiento, evitando duplicar operaciones con efectos laterales.

Aspectos que deberá cubrir:

- Reintentar únicamente errores transitorios identificados, como timeouts, límites de
  capacidad y determinadas respuestas `429` o `5xx`.
- Usar backoff exponencial con jitter y respetar `Retry-After` cuando exista.
- Configurar número máximo de intentos y presupuesto total de tiempo.
- No reintentar automáticamente tools no idempotentes.
- Incorporar claves de idempotencia para operaciones que puedan repetirse de forma
  segura.
- Registrar cada intento sin incluir argumentos o resultados sensibles.
- Propagar cancelaciones del cliente sin convertirlas en reintentos.
- Añadir métricas de intentos, recuperación, agotamiento y latencia acumulada.

## Hooks de observabilidad y alertas

Añadir un mecanismo desacoplado para reaccionar ante excepciones personalizadas o
eventos críticos registrados por la aplicación. El logger deberá producir un evento
estructurado y uno o varios adaptadores podrán enviarlo por email, webhook, Slack u
otro canal sin acoplar el dominio al proveedor de notificaciones.

Aspectos que deberá cubrir:

- Definir un puerto como `ErrorEventPublisher` o `AlertSink` independiente del logger y
  de la API concreta de email.
- Crear un catálogo explícito de excepciones y severidades que generan alertas; no
  enviar notificaciones por cualquier error indiscriminadamente.
- Recopilar `request_id`, `conversation_id`, `session_id` y usuario cuando sea
  seguro, nombre de la excepción, mensaje sanitizado, stack trace, endpoint, proveedor,
  modelo y timestamps.
- Propagar el contexto mediante `structlog.contextvars` para que logger y publisher
  compartan identificadores de correlación.
- Aplicar redacción de API keys, tokens, mensajes, argumentos y resultados sensibles
  antes de construir el evento o adjuntar logs.
- Ejecutar el envío fuera del camino crítico de la request mediante una cola o tarea
  controlada, con timeout y política de reintentos propia.
- Evitar bucles: un fallo al enviar una alerta no deberá generar otra alerta idéntica.
- Añadir deduplicación, rate limiting y ventanas de agrupación para impedir tormentas
  de emails ante un fallo repetido.
- Registrar el resultado del envío sin bloquear ni modificar la excepción original.
- Permitir múltiples sinks configurables y una implementación `NoOpAlertSink` para
  entornos donde las alertas estén desactivadas.

Pruebas previstas:

- Mockear el cliente de la API de email o webhook sin realizar comunicaciones reales.
- Provocar una excepción personalizada y comprobar que se publica exactamente un
  evento.
- Verificar que el evento contiene todos los identificadores de correlación y metadatos
  necesarios.
- Confirmar que secretos, argumentos de tools y contenido sensible están redactados.
- Simular timeout y error del proveedor de alertas y comprobar que la respuesta
  principal no queda bloqueada ni sustituida.
- Verificar deduplicación y rate limiting para errores repetidos.

## Criterios transversales

Antes de considerar terminada cualquiera de estas líneas:

- Debe incluir tests unitarios y de integración.
- Debe funcionar tanto en respuestas completas como en streaming.
- No debe introducir formatos específicos de proveedor en dominio o aplicación.
- Debe documentar límites, configuración, observabilidad y tratamiento de errores.
- Debe preservar el aislamiento entre usuarios.
