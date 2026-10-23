# Resumen: diagrama y alertas críticas

El diagrama usa SVG propio y mediciones HTML. El frontal de la ilustración muestra **FORZA** y el modelo **NT-2402PRO**, sin visor LCD. La iluminación de su botón de encendido refleja el estado del suministro; su estado accesible acompaña los cambios de conexión y calidad de lectura.

El voltaje de entrada y salida son lecturas distintas; `frequency_hz` conserva el contrato existente y se presenta junto a la salida, sin duplicarlo como frecuencia de entrada. La carga del UPS representa `load_percent`, separada del porcentaje de batería. El llenado visual se limita a 100%, pero el texto conserva una sobrecarga reportada superior a ese límite. Los valores ausentes se presentan como no disponibles, incluido su texto accesible.

## Calidad y recorrido

Una muestra es actual si el servicio está disponible, la conexión está confirmada y su timestamp tiene una edad entre -5 y 15 segundos. El temporizador de interfaz existente revisa la caducidad entre sondeos y al recuperar foco o visibilidad. No se agregan consultas periódicas.

Los conductos rectos usan un contorno fino y pulsos de luz con estela, sin puntas de flecha. La entrada de red es turquesa; la rama de batería y la salida hacia los equipos son azules. En red se activa entrada→UPS→equipos. En batería se activa batería→UPS→equipos, con el pulso de la rama recta ascendiendo desde la batería hasta el UPS; la entrada de red permanece neutra.

La animación exige una muestra actual, un origen de suministro confirmado y una salida con energía. Una falla, un origen desconocido, datos antiguos o sin confirmar y una salida de cero voltios neutralizan las rutas. La batería en red permanece neutra: la lectura no demuestra una carga activa ni un bypass. Su porcentaje conserva la precedencia reportado→estimado, con su etiqueta de procedencia y el voltaje visible. Desde 0.9.17, la protección por porcentaje puede usar la estimación conocida y explícita para abreviar la cuenta antes de hibernar. Las estimaciones no se convierten en minutos de autonomía ni en una señal de alarma del dispositivo.

## Modal de condiciones actuales

La ventana global utiliza únicamente muestras actuales: falla reportada, carga del UPS ≥100%, señal de batería baja o porcentaje reportado ≤20%. No deriva alarmas del historial ni de la última notificación de Windows. Los valores individuales desconocidos tampoco confirman una recuperación. Una nueva ventana agrupa condiciones y prioriza falla, sobrecarga y batería baja.

Los incidentes pendientes se guardan y permanecen hasta pulsar **Entendido**, incluso tras recuperar el suministro, recargar o volver a abrir el panel. Escape no reconoce el incidente. Reconocerlo no cancela una cuenta atrás ni cambia la configuración. Si una condición pendiente se recupera, el diálogo informa que está resuelta; si pierde confirmación, indica incertidumbre. Una nueva incidencia se conserva independientemente del reconocimiento anterior.

Desde 0.9.19 una alerta por carga elevada no bloquea por sí sola el apagado del UPS. La sección Protección muestra los cuatro grupos efectivos de bloqueo y el motivo específico; el último bloqueo histórico se distingue de las condiciones actuales y también conserva su aviso hasta Entendido.

La cuenta atrás y su cancelación usan el estado y endpoint existentes. Se confirma la cancelación mediante respuesta del monitor, y una cuenta nueva invalida mensajes de éxito anteriores. El diálogo conserva el foco, permite recorrer sus controles por teclado y restaura el foco al reconocerlo. Los avisos de Windows y sus preferencias permanecen independientes; el modal no reproduce sonido.

## Tema y adaptación

El selector ocupa el ancho útil del lateral y una fila completa en móvil. La transición de 350 ms solo se habilita al cambiar manualmente de tema. La preferencia sigue siendo `elipro-theme`; la primera pintura no se anima. Movimiento reducido desactiva las transiciones y las animaciones del diagrama.

En un escritorio amplio de referencia de 1920 × 1080, Resumen reúne el diagrama compacto arriba y las tarjetas completas de Mediciones y Eventos recientes debajo. El gráfico conserva su período y sus tres métricas; la lista de eventos mantiene su filtro. La gráfica sigue compartida con Historial y los eventos recientes permanecen exclusivos de Resumen.

La composición se adapta al ancho disponible y apila las tarjetas cuando falta espacio. Móviles, ventanas bajas y avisos o contenido adicional permiten desplazamiento. El ajuste de altura nunca recorta alertas, errores, la cuenta atrás de protección ni sus controles. `prefers-reduced-motion` desactiva los pulsos y las transiciones del diagrama, conservando colores, datos y estados estáticos. Se mantienen la API, el lector, los datos guardados y las reglas de protección.
