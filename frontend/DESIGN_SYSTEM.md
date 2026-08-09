# Sistema visual de TesseraFlow

La identidad visual vive en `src/theme.css`. Los componentes deben consumir
tokens semánticos, nunca colores hexadecimales directos.

## Aplicar color a un componente

```css
.card {
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
}

.card-action {
  color: var(--color-brand-contrast);
  background: var(--color-brand);
}

.card-action:hover {
  background: var(--color-brand-hover);
}
```

Usa `brand` para acciones y selección, `surface` para fondos, `text` para
jerarquía tipográfica y los grupos `success`, `warning`, `danger` e `info` para
estados. La navegación oscura usa exclusivamente tokens `--color-nav-*`.

## Cambiar la identidad corporativa

1. Cambia la escala `--brand-*` en `src/theme.css`.
2. Ajusta `--color-nav` si el fondo de navegación necesita otro tono.
3. Revisa `--chart-*` para que las visualizaciones armonicen con la nueva marca.

Los aliases del final de `theme.css` existen para compatibilidad y pueden
retirarse gradualmente cuando los estilos se separen por componente.
