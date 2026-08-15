interface BrandMarkProps {
  large?: boolean
}

/** Render the shared product mark while keeping it decorative beside visible brand copy. */
export function BrandMark({ large = false }: BrandMarkProps) {
  return (
    <img
      className={`brand-mark${large ? ' large' : ''}`}
      src="/tesseraflow-mark.svg"
      alt=""
      aria-hidden="true"
    />
  )
}
