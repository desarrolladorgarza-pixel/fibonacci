## Qué cambia

<!-- Y por qué. Si corrige un issue, enlázalo. -->

## Lista de verificación

- [ ] `make check` pasa (ruff + 220 pruebas)
- [ ] Añadí pruebas para lo nuevo, o para el bug que corrijo
- [ ] No añadí dependencias en tiempo de ejecución (o expliqué por qué era
      inevitable)
- [ ] Ninguna prueba nueva toca red externa, el home real, ni requiere un LLM

## Si añadiste una herramienta

- [ ] Declara `mutating` correctamente
- [ ] Si muta y es reversible, provee `undo=`
- [ ] Si es irreversible, está marcada `reversible=False` y lo documenta

## Si tocaste algo de seguridad

<!-- Gate, journal, identidad, redacción, control de salida, bóveda. -->

- [ ] Expliqué el razonamiento en la descripción
- [ ] No relajé una compuerta sin discutirlo antes en un issue
