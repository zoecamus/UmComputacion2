# Dudas pendientes

- No llegué a implementar los analizadores de FDs, threads, señales y
  scheduling por falta de tiempo. Entiendo el patrón (mismo esquema que
  resumen/memoria/sistema) pero no llegué a escribirlos ni probarlos.
- No implementé Lock explícito en ningún lado porque cada analizador
  escribe una clave distinta del snapshot. Tengo que revisar en clase de
  Sincronización si esto es realmente seguro o si el Manager ya serializa
  internamente todas las operaciones aunque sean sobre claves distintas.
- `SIGHUP` (reload de config.json) no está implementado.
- No entiendo del todo la diferencia entre `Value` (memoria compartida
  cruda) y `Manager.Value` — usé el primero acá, quiero confirmar si es la
  elección correcta para un intervalo simple tipo float.