# Roadmap técnico

Este documento recoge funcionalidades deliberadamente fuera del alcance de la fase
actual. Su implementación deberá conservar los contratos neutrales al proveedor, el
aislamiento entre usuarios y la separación entre dominio, aplicación y adaptadores.

## Componentes visuales

Permitir que el backend envíe componentes visuales estructurados a un futuro frontend
como eventos del stream, además de texto y eventos de tools.

Aspectos que deberá cubrir:

- Definir eventos neutrales como `visual_component` con un esquema versionado.
- Empezar con un conjunto limitado de componentes permitidos, por ejemplo tablas,
  tarjetas, avisos, métricas y formularios de confirmación.
- Validar los payloads en el backend antes de enviarlos.
- Mantener separada la descripción semántica del componente de su implementación
  concreta en React, Vue u otro framework.
- Evitar HTML o JavaScript arbitrario generado por el modelo.
- Añadir compatibilidad con SSE y, si se necesita interacción bidireccional, evaluar
  WebSocket.
- Permitir que clientes sin soporte visual degraden el contenido a texto.

## Orquestación multiagente por capas

Diseñar una arquitectura en la que varias capas de agentes puedan trabajar de forma
concurrente sobre una misma solicitud, sin acoplar la aplicación a un proveedor de
modelos concreto. Una primera capa interactiva y de baja latencia se encargará de
generar la respuesta inmediata o los componentes visuales, mientras que una segunda
capa ejecutará en segundo plano los trabajos más pesados que requieran mayor
razonamiento, contexto o tiempo de procesamiento.

Aspectos que deberá cubrir:

- Definir contratos neutrales para describir tareas, dependencias, prioridades,
  resultados parciales y estados como `queued`, `running`, `completed`, `failed` y
  `cancelled`.
- Incorporar un orquestador en la capa de aplicación que distribuya el trabajo por
  capacidades y coste esperado, sin comprobar nombres de proveedores o modelos.
- Mantener la capa interactiva dentro de un presupuesto explícito de latencia y evitar
  que espere innecesariamente a los trabajos pesados.
- Ejecutar los trabajos de razonamiento pesado mediante una cola y workers en segundo
  plano, con límites de concurrencia, tiempo, memoria, rondas y consumo por usuario o
  tenant.
- Permitir que ambas capas trabajen a la vez y publiquen resultados parciales mediante
  eventos neutrales, conservando la trazabilidad con `request_id`, `conversation_id` y
  `job_id`.
- Definir cómo un resultado de segundo plano complementa, corrige o sustituye una
  respuesta previa sin producir actualizaciones incoherentes ni duplicadas.
- Versionar los resultados y aplicar control de concurrencia para descartar entregas
  tardías u obsoletas.
- Persistir únicamente el estado mínimo necesario para recuperar, consultar o cancelar
  trabajos, sin guardar contexto de usuario en agentes o servicios compartidos.
- Exponer mecanismos para consultar el estado y recibir la finalización de un trabajo;
  evaluar SSE, WebSocket, polling o webhooks según las necesidades del cliente.
- Propagar cancelaciones cuando sea posible y definir políticas explícitas para tareas
  que deban continuar tras la desconexión del cliente.
- Aplicar autenticación, autorización y aislamiento multiusuario tanto al envío del
  trabajo como a la consulta, cancelación y entrega de sus resultados.
- Añadir observabilidad de tiempos en cola, latencia de la primera respuesta, duración
  del razonamiento, uso de recursos, errores y resultados descartados, sin registrar
  datos personales ni contenido sensible.
- Probar el aislamiento entre ejecuciones concurrentes, la recuperación tras reinicios,
  la cancelación, los timeouts, los resultados fuera de orden y los fallos parciales de
  cada capa.

## Política de ejecución de tools en batch

Introducir una política explícita para decidir cuándo varias tools pueden ejecutarse en
paralelo, secuencialmente o como una operación coordinada.

Aspectos que deberá cubrir:

- Añadir metadatos como `concurrency_safe`, `read_only`, `idempotent` y grupo de
  exclusión mutua a cada tool.
- Ejecutar en paralelo únicamente tools declaradas como seguras para concurrencia.
- Mantener orden secuencial para operaciones con dependencias o efectos laterales.
- Definir límites de concurrencia globales, por usuario, tenant y proveedor externo.
- Preservar la correspondencia entre cada `call_id` y su resultado.
- Definir comportamiento ante éxito parcial y cancelación de un batch.
- Evitar paralelizar operaciones con efectos laterales que compitan sobre el mismo recurso.

## Soporte para modelos STS

Añadir soporte para modelos *speech-to-speech* (STS), capaces de recibir y generar
audio en tiempo real sin convertir la experiencia pública en una integración acoplada
a un proveedor concreto.

Aspectos que deberá cubrir:

- Definir eventos neutrales para audio de entrada, audio de salida, transcripciones,
  turnos de conversación y errores.
- Evaluar WebSocket o WebRTC para la comunicación bidireccional de baja latencia.
- Mantener clientes y sesiones específicos de OpenAI, Gemini u otros proveedores
  dentro de sus respectivos adaptadores.
- Gestionar detección de voz, inicio y final de turno, interrupciones y cancelación de
  una respuesta en curso.
- Permitir tool calls durante una sesión de voz y comunicar su estado sin bloquear la
  reproducción de audio innecesariamente.
- Definir codecs, frecuencia de muestreo, tamaño de fragmentos y límites de duración.
- Aplicar backpressure y límites de memoria para clientes o redes lentas.
- Establecer políticas explícitas para grabación, retención, transcripción y borrado de
  audio potencialmente sensible.
- Proporcionar degradación a texto cuando el cliente o el modelo no soporte STS.
- Añadir métricas de latencia hasta el primer audio, interrupciones, errores y duración
  de sesión.

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
- Recopilar `request_id`, `conversation_id`, `session_id`, usuario o tenant cuando sea
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
- Debe contemplar autenticación, autorización y aislamiento multiusuario.
