# Arquitectura de confiabilidad

## Adquisición y persistencia

El hilo `ups-hardware-polling` solo lee el adaptador y publica copias de las muestras en memoria. Un consumidor separado evalúa la protección, genera avisos y solicita persistencia. No se escribe SQLite ni se espera a una ventana desde el hilo de adquisición.

`TelemetryRingBuffer` conserva 256 muestras por defecto. Usa una `SimpleQueue` con un único token de despertar; los datos están en deques limitadas. Un consumidor lento no crea una cola ilimitada. Si se supera la capacidad, conserva contadores y la primera/última fecha de cada tipo conocido de transición, e informa del descarte.

Los datos atrasados no se reproducen como decisiones actuales. Después de retraso o desbordamiento se reinicia la continuidad del motor. Solo la última muestra reciente puede iniciar una acción; el motor vuelve a comprobar su secuencia antes de actuar. Las transiciones intermedias se registran como hechos históricos parciales (`telemetry_delayed_transition`), no como una cronología completa de incidentes.

Las transacciones QS y la enumeración de metadatos HID se ejecutan en procesos internos de propósito fijo. El reporte overlapped tiene un plazo de 2,5 segundos; el padre limita la respuesta del proceso a 3 segundos y la espera de terminación a otros 0,5 segundos. Las API síncronas del controlador no ofrecen cancelación garantizada: si no se confirma la salida del proceso, la ruta USB queda en cuarentena y no se abre otro lector para ella. La reconexión usa espera exponencial limitada a 30 segundos. El transporte sigue permitiendo exclusivamente la consulta de estado QS.

La escritura normal agrupa hasta 32 registros o 30 segundos en una transacción. Las transiciones críticas y las alarmas solicitan inmediatamente al trabajador de persistencia que guarde todos los registros anteriores en orden. SQLite usa WAL y `synchronous=FULL`; las decisiones no esperan al disco. Esto es una solicitud de volcado inmediato, no una garantía frente a un disco fallido o a la terminación del proceso antes del commit. Los ajustes conservan confirmación síncrona.

El cierre no libera la exclusión del monitor ni cierra SQLite mientras siga vivo el lector, el consumidor o el supervisor. Las pruebas usan hardware simulado, bases temporales y callbacks de energía inofensivos.
