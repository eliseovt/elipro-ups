# Arquitectura de confiabilidad

## Adquisición y persistencia

El hilo `ups-hardware-polling` solo lee el adaptador y publica copias de las muestras en memoria. Un consumidor separado evalúa la protección, genera avisos y solicita persistencia. No se escribe SQLite ni se espera a una ventana desde el hilo de adquisición.

`TelemetryRingBuffer` conserva 256 muestras por defecto. Usa una `SimpleQueue` con un único token de despertar; los datos están en deques limitadas. Un consumidor lento no crea una cola ilimitada. Si se supera la capacidad, conserva contadores y la primera/última fecha de cada tipo conocido de transición, e informa del descarte.

Los datos atrasados no se reproducen como decisiones actuales. Después de retraso o desbordamiento se reinicia la continuidad del motor. Solo la última muestra reciente puede iniciar una acción; el motor vuelve a comprobar su secuencia antes de actuar. Las transiciones intermedias se registran como hechos históricos parciales (`telemetry_delayed_transition`), no como una cronología completa de incidentes.

Las transacciones QS y la enumeración de metadatos HID se ejecutan en procesos internos de propósito fijo. El reporte overlapped tiene un plazo de 2,5 segundos; el padre limita la respuesta del proceso a 3 segundos y la espera de terminación a otros 0,5 segundos. Las API síncronas del controlador no ofrecen cancelación garantizada: si no se confirma la salida del proceso, la ruta USB queda en cuarentena y no se abre otro lector para ella. La reconexión usa espera exponencial limitada a 30 segundos. El sondeo habitual permite exclusivamente QS. El diagnóstico explícito de batería tiene una lista separada con BT?/BL?, utiliza los mismos límites y exclusión, y no activa protección ni persistencia.

Antes de cada lectura nativa, la consulta local `VP_LINK` distingue tres resultados: respuesta exacta `monitorOK` (Forza confirmado), puerto inaccesible explícito (ausencia confirmada) e incertidumbre. Los errores al crear, configurar o cerrar el socket no prueban ausencia. Solo la incertidumbre se reintenta: hasta tres consultas con un timeout de 0,3 segundos y dos pausas de 0,1 segundos. Si persiste, se descarta el transporte y se conserva el bloqueo del USB con un motivo y códigos numéricos; no se presenta como Forza confirmado. No se guarda en caché la ausencia ni se suprimen avisos de pérdidas reales. La selección inicial `auto` conserva su comportamiento conservador hacia el puente ante incertidumbre.

La escritura normal agrupa hasta 32 registros o 30 segundos en una transacción. Las transiciones críticas y las alarmas solicitan inmediatamente al trabajador de persistencia que guarde todos los registros anteriores en orden. SQLite usa WAL y `synchronous=FULL`; las decisiones no esperan al disco. Esto es una solicitud de volcado inmediato, no una garantía frente a un disco fallido o a la terminación del proceso antes del commit. Los ajustes conservan confirmación síncrona.

El cierre no libera la exclusión del monitor ni cierra SQLite mientras siga vivo el lector, el consumidor o el supervisor. Las pruebas usan hardware simulado, bases temporales y callbacks de energía inofensivos.

## Servicio, sesiones y avisos

El servicio es dueño del motor de protección y del callback de energía. El agente interactivo solo recibe avisos y puede solicitar una cancelación. El servicio puede evaluar una batería baja sin ningún agente registrado. La capacidad administrativa `--allow-power-actions` y la política `enabled` son bloqueos independientes; el instalador no habilita ninguno. El modo de prueba del servicio tampoco permite esa capacidad.

El protocolo de avisos v2 separa el cursor de lectura del acuse de entrega. Incluye una época de servicio para rechazar acuses de una ejecución anterior. Un acuse solo procede de una respuesta positiva del backend nativo, después de solicitar un aviso a Windows o mostrar la ventana propia. La aceptación en la cola Python, un sonido o un cursor adelantado no son acuses. Tampoco se afirma que el usuario haya leído el aviso.

El agente consulta WTS para su propia sesión. Una sesión bloqueada, desconectada o de estado desconocido no permite entrega. Al reanudar, el siguiente sondeo —normalmente en un segundo— recupera los avisos retenidos. La retención es limitada, por defecto 32 eventos y 24 horas, y solo existe en memoria hasta que termina el servicio; el historial de disco es independiente. Los diagnósticos informan del descarte por capacidad. No se promete entrega exactamente una vez a través de un cierre o fallo de Windows.

El broker vuelve a construir los avisos a partir del contexto reciente. Un corte recuperado se presenta como registro anterior y una cuenta vencida o cancelada no reutiliza sus segundos antiguos. El notificador invalida tickets pendientes cuando cambia su contenido o la sesión deja de estar disponible. Los acuses perdidos se reintentan y el agente limita los avisos en vuelo.

El notificador Python y el lanzador C# solicitan PMv2 antes de crear ventanas. Las ventanas propias ajustan geometría y fuentes y procesan `WM_DPICHANGED`. Las API ausentes o un contexto ya fijado no impiden iniciar el agente. Estas pruebas de API no sustituyen una revisión visual en el monitor y escalado de destino.

Referencias: [DPI de proceso](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setprocessdpiawarenesscontext), [estado WTSInfoEx](https://learn.microsoft.com/en-us/windows/win32/api/wtsapi32/ns-wtsapi32-wtsinfoex_level1_w) y [programas de servicio](https://learn.microsoft.com/en-us/windows/win32/services/service-programs).

## Empaquetado y CI

PyInstaller genera un directorio con dependencias en `_internal`, instalado en Program Files. No se utiliza un ejecutable autoextraíble `onefile`. Las herramientas Python de compilación están fijadas con versión y SHA256 en `requirements-build.txt`; CI las instala con `--require-hashes --only-binary=:all:`. UPX permanece desactivado y no se incorporan datos de usuario.

La comprobación del instalador está restringida al runner Windows x64 desechable. Su modo de simulación ejecuta el monitor, API, historial y protección reales con un adaptador artificial y timestamps recientes. El adaptador, el monitor y el host del servicio fuerzan el bloqueo de acciones reales. Se comprueban la cuenta LocalSystem, Sesión 0, transiciones eléctricas, una acción simulada, actualización y conservación de datos al desinstalar. No se requiere agente gráfico ni UPS físico.

La firma es opcional y usa una huella explícita del almacén personal de certificados. SignTool utiliza SHA256, sellado RFC3161 y verificación Authenticode; la verificación comprueba además la huella seleccionada y el certificado de sellado. Se firman los ejecutables propios, Setup y el desinstalador; las dependencias de otros proveedores conservan sus firmas. El checksum se calcula después de la firma. Un error de firma o verificación detiene la compilación solicitada, sin rebajarla silenciosamente a una compilación sin firmar.

Sin certificado configurado se genera un informe explícito de distribución sin firmar. `-RequireSigning` exige todos los requisitos de firma. No se crean certificados ni raíces de confianza para superar una prueba. La ruta CI con secretos requiere ejecución manual sobre una referencia admitida y un entorno de firma protegido; importa el PFX temporalmente y limpia su archivo y clave privada en `finally`. El entorno requiere configuración administrativa de revisores y restricciones antes de utilizarlo. No se ha validado una firma real mientras no se disponga de un certificado.

Referencias: [SignTool](https://learn.microsoft.com/en-us/windows/win32/seccrypto/signtool) y [entornos protegidos de GitHub Actions](https://docs.github.com/en/actions/deployment/targeting-different-environments/managing-environments-for-deployment).
