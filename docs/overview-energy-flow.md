# Resumen: diagrama y alertas críticas

El diagrama usa SVG propio y mediciones HTML. El voltaje de entrada y salida son lecturas distintas; `frequency_hz` conserva el contrato existente y se presenta junto a la salida, sin duplicarlo como frecuencia de entrada. La barra del frontal representa `load_percent`, no el nivel de batería. El llenado visual se limita a 100%, pero el texto conserva una sobrecarga reportada superior a ese límite. Los valores ausentes se presentan como no disponibles, incluido su texto accesible.

## Calidad y recorrido

Una muestra es actual si el servicio está disponible, la conexión está confirmada y su timestamp tiene una edad entre -5 y 15 segundos. El temporizador de interfaz existente revisa la caducidad entre sondeos y al recuperar foco o visibilidad. No se agregan consultas periódicas.

En red se activa entrada→UPS→equipos. En batería se activa batería→UPS→equipos. Una falla, un origen desconocido o datos sin confirmar neutralizan las rutas. La batería en red no representa una carga activa ni un bypass. Su porcentaje conserva la precedencia reportado→estimado, con su etiqueta de procedencia. Las estimaciones nunca generan alertas ni decisiones de energía.

## Modal de condiciones actuales

La ventana global utiliza únicamente muestras actuales: falla reportada, carga del UPS ≥100%, señal de batería baja o porcentaje reportado ≤20%. No deriva alarmas del historial ni de la última notificación de Windows. Los valores individuales desconocidos tampoco confirman una recuperación. Una nueva ventana agrupa condiciones y prioriza falla, sobrecarga y batería baja.

El reconocimiento se guarda solo en memoria durante la navegación. Entendido y Escape cierran el aviso visual; no cancelan una cuenta atrás ni cambian la configuración. Una lectura que confirma la recuperación rearma la condición para otro incidente. Si una condición pendiente se recupera, el diálogo informa que está resuelta; si pierde confirmación, indica incertidumbre. No se vuelve a abrir en cada sondeo ni se reproduce otro toast web para la misma alarma.

La cuenta atrás y su cancelación usan el estado y endpoint existentes. Se confirma la cancelación mediante respuesta del monitor, y una cuenta nueva invalida mensajes de éxito anteriores. El diálogo conserva el foco, permite recorrer sus controles por teclado y restaura el foco al reconocerlo. Los avisos de Windows y sus preferencias permanecen independientes; el modal no reproduce sonido.

## Tema y adaptación

El selector ocupa el ancho útil del lateral y una fila completa en móvil. La transición de 350 ms solo se habilita al cambiar manualmente de tema. La preferencia sigue siendo `elipro-theme`; la primera pintura no se anima. Movimiento reducido desactiva las transiciones y las animaciones del diagrama.

Hasta 600 px se utiliza un recorrido vertical con batería al costado. El gráfico y el registro conservan una sola instancia compartida y se apilan cuando falta espacio. Las ventanas bajas permiten desplazamiento; no se modifican la API, el lector, los datos guardados ni las reglas de protección.
