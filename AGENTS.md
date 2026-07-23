# AGENTS.md

## Propósito del repositorio

Este proyecto implementa un agente multiusuario con FastAPI, Python y proveedores de
modelos intercambiables. La prioridad arquitectónica es mantener un núcleo sencillo,
independiente de frameworks y proveedores, con interfaces pequeñas que permitan
incorporar nuevas capacidades sin propagar cambios por todo el sistema.

Consulta `ROADMAP.md` antes de implementar componentes visuales, nuevas extensiones STS,
reintentos o alertas.

## Principios de diseño

### Separación de responsabilidades

Cada módulo debe tener un motivo principal para cambiar:

- `domain`: conceptos y eventos neutrales al proveedor. No importa FastAPI, OpenAI,
  Redis ni otros adaptadores.
- `application`: casos de uso, orquestación y puertos. Decide qué debe ocurrir, no cómo
  habla una API externa.
- `infrastructure`: traducciones y clientes concretos, como OpenAI, Gemini, Redis o un
  proveedor de email.
- `api`: contratos y transporte HTTP/SSE. Convierte entre schemas públicos y modelos de
  aplicación.
- `tools`: implementaciones independientes de capacidades invocables por el modelo.
- `bootstrap.py`: único punto de composición de implementaciones concretas y recursos
  compartidos.
- `main.py`: creación de FastAPI y ciclo de vida; debe permanecer pequeño.

No mezcles transporte HTTP, reglas de aplicación, formatos de proveedor y acceso a
datos en una misma clase o función.

### Dirección de dependencias

Las dependencias apuntan hacia el núcleo:

```text
api ----------> application <---------- infrastructure
                     |
                     v
                   domain
```

- `domain` no depende de ninguna otra capa del proyecto.
- `application` puede depender de `domain`, pero no de `api` ni `infrastructure`.
- Los adaptadores implementan puertos definidos por el núcleo.
- El núcleo nunca debe comprobar si el proveedor es OpenAI, Gemini u otro mediante
  condicionales.

Antes de finalizar un cambio, busca nombres y formatos específicos del proveedor fuera
de `infrastructure`. Elementos como `function_call_output`, eventos de Responses API o
clases del SDK de OpenAI no deben aparecer en dominio o aplicación.

## Interfaces con valor

Las interfaces deben representar capacidades relevantes, no envolver cada clase por
costumbre.

Crea o modifica un puerto cuando:

- Exista más de una implementación actual o prevista de forma concreta.
- Sea una frontera con un sistema externo.
- Permita probar un caso de uso sin red, disco o servicios reales.
- Proteja al núcleo de formatos o ciclos de vida ajenos.

Evita interfaces que:

- Solo repliquen todos los métodos de una implementación concreta.
- Expongan diccionarios opacos del proveedor.
- Tengan demasiadas responsabilidades o parámetros no relacionados.
- Se creen únicamente para anticipar posibilidades hipotéticas sin un caso de uso.

Prefiere contratos pequeños, tipados y con semántica propia, como `ModelGateway`,
`ModelSession`, `ToolSpec`, `ToolCall` y `ToolResult`.

## Control de complejidad

- Implementa la solución mínima que preserve los límites arquitectónicos.
- Prefiere composición frente a herencia, salvo que exista una relación estable y
  clara.
- Extrae una función o clase cuando reduzca carga cognitiva o aísle una responsabilidad,
  no únicamente para reducir líneas.
- Evita factories, managers, services o repositorios genéricos sin una responsabilidad
  concreta.
- No añadas estado mutable compartido si el estado puede vivir en una ejecución o
  sesión local.
- Usa dataclasses inmutables para definiciones, comandos, resultados y eventos cuando
  sea apropiado.
- Haz explícitos los límites de iteración, concurrencia, memoria, tiempo y reintentos.
- Una optimización debe responder a una necesidad medible; documenta su tradeoff.

Si una funcionalidad requiere modificar varias capas, primero define el contrato neutral
y después adapta cada borde. No filtres detalles externos hacia el núcleo para ahorrar
una traducción.

## Ciclos de vida y concurrencia

- Crea una instancia de clientes costosos y pools, como `AsyncOpenAI`, por proceso en el
  `lifespan`.
- Cierra esos recursos durante el apagado mediante el contenedor de aplicación.
- Crea una `ModelSession` ligera por ejecución para aislar mensajes, respuestas y tool
  calls entre usuarios concurrentes.
- No guardes usuario, conversación, resultados ni `response_id` actual en servicios
  compartidos.
- Las variables específicas de una request deben ser locales o pertenecer a su sesión.
- Recuerda que varios workers implican varios procesos, contenedores y pools.
- Las tools solicitadas en una misma respuesta se ejecutan concurrentemente y sus
  resultados deben conservar la correspondencia con cada llamada.
- Propaga cancelaciones; no conviertas una desconexión del cliente en un error
  recuperable o un reintento.

## Proveedores de modelos

Cada proveedor debe tener su propio adaptador en `infrastructure` y traducir entre su
SDK y los contratos neutrales.

Un adaptador debe encargarse de:

- Serializar `AgentDefinition` y `ToolSpec` al formato del proveedor.
- Convertir respuestas y tool calls a `ModelReply` y `ToolCall`.
- Traducir `ToolResult` al formato de continuación correspondiente.
- Acumular fragmentos de argumentos antes de exponer un tool call completo.
- Gestionar el contexto o estado efímero específico del proveedor dentro de
  `ModelSession`.
- Cerrar streams concretos sin cerrar el cliente compartido.

Añadir Gemini u otro proveedor no debe exigir cambios en `AgentService`.

## Tools

- Cada tool implementa una sola capacidad y declara argumentos mediante
  `ToolArguments`.
- Los argumentos emitidos por el modelo son entrada no confiable y siempre se validan.
- Los nombres de tools son únicos, estables y descriptivos.
- Una `AgentDefinition` solo puede exponer tools registradas y autorizadas.
- No uses `eval`, ejecución arbitraria ni acceso más amplio del necesario.
- No guardes contexto de usuario en atributos mutables de una instancia compartida.
- Para futuras tools multiusuario, pasa un contexto de ejecución explícito y tipado.
- Los errores esperables de una tool se convierten en resultados estructurados; las
  cancelaciones deben propagarse.
- No reintentes automáticamente operaciones no idempotentes.

## Streaming y eventos

- Los eventos públicos y de aplicación son neutrales al proveedor.
- `OpenAIModelSession` puede conocer eventos OpenAI; `AgentService` y FastAPI no.
- Todo stream exitoso termina con un único evento terminal `completed`.
- Los argumentos fragmentados de una tool no se ejecutan hasta estar completos.
- Usa context managers para liberar streams ante éxito, error o desconexión.
- SSE es el transporte actual para texto unidireccional. Evalúa WebSocket o WebRTC solo
  cuando exista una necesidad bidireccional, como STS o interrupciones en tiempo real.
- Mantén separada la semántica de un evento de su representación SSE o futura UI.
- Considera backpressure y evita buffers sin límites.

## Errores, logging y seguridad

- Usa logs estructurados y conserva `request_id` en el contexto.
- No registres mensajes, argumentos de tools, resultados, tokens, claves ni datos
  sensibles salvo que exista una política explícita y segura.
- Registra identificadores, tipo de evento, duración, proveedor, modelo y estado.
- Define excepciones específicas cuando el consumidor pueda tratarlas de forma
  diferente.
- No captures excepciones de forma amplia salvo en una frontera donde se conviertan en
  un resultado o evento intencionado.
- Nunca ocultes cancelaciones.
- Los futuros hooks de alertas deben depender de un puerto como `AlertSink`, redactar
  secretos y ejecutarse fuera del camino crítico.
- Un fallo al notificar una alerta no debe reemplazar el error original ni crear un
  bucle de alertas.

## API y compatibilidad

- Valida toda entrada HTTP con Pydantic.
- No expongas directamente modelos del SDK o excepciones internas.
- Mantén `/v1/agent/run` y `/v1/agent/stream` semánticamente alineados.
- Un cambio incompatible requiere una nueva versión o una estrategia de migración.
- Los errores emitidos después de iniciar SSE deben convertirse en eventos seguros; los
  detalles internos permanecen en logs.
- Incluye identificadores de correlación en respuestas y eventos cuando corresponda.

## Tests

Todo cambio de comportamiento debe incluir tests proporcionales al riesgo:

- Tests del caso de uso con puertos simulados, sin llamadas reales a proveedores.
- Tests de adaptadores que comprueben traducción en ambas direcciones.
- Tests de aislamiento entre sesiones y requests concurrentes.
- Tests de tool calls, errores, límites de rondas y cancelaciones.
- Tests equivalentes para respuesta completa y streaming cuando aplique.
- APIs de email, webhooks, Redis y proveedores deben mockearse en tests unitarios.

No hagas depender la suite normal de API keys, red o servicios externos. Los tests de
integración reales deben ser explícitos, opcionales y estar documentados.

## Documentación y docstrings

- Todas las clases y funciones propias deben tener docstrings útiles.
- Documenta responsabilidades, invariantes y efectos relevantes; evita repetir el
  nombre del símbolo sin aportar contexto.
- Actualiza `README.md` cuando cambie el uso actual.
- Actualiza `ROADMAP.md` para trabajo futuro, no para presentar como disponible una
  funcionalidad que todavía no existe.
- Incluye ejemplos mínimos de consumo para nuevos endpoints o eventos.

## Comandos de verificación

Usa el entorno virtual local:

```bash
source .venv/bin/activate
```

Antes de finalizar cambios de código ejecuta:

```bash
ruff format --check .
ruff check .
mypy src
pytest
git diff --check
```

Para ejecutar conjuntamente lint, tipado y tests usa:

```bash
make check
```

## Definición de terminado

Un cambio está terminado cuando:

- La responsabilidad nueva tiene una ubicación clara.
- Las dependencias respetan la dirección hacia el núcleo.
- Los contratos no filtran formatos de infraestructura.
- Concurrencia, cancelación, errores y seguridad están contemplados.
- Existen tests del comportamiento y de las fronteras modificadas.
- Ruff, mypy, pytest y `git diff --check` pasan.
- README, roadmap y docstrings reflejan correctamente el estado real.
