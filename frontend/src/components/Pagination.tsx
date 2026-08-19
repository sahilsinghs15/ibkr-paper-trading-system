interface PaginationProps {
  currentPage: number
  totalItems: number
  pageSize: number
  onPageChange: (page: number) => void
  onPageSizeChange?: (size: number) => void
  pageSizeOptions?: number[]
  compact?: boolean
}

export function Pagination({
  currentPage,
  totalItems,
  pageSize,
  onPageChange,
  onPageSizeChange,
  pageSizeOptions = [5, 10, 25],
  compact = false,
}: PaginationProps) {
  const totalPages = Math.max(1, Math.ceil(totalItems / pageSize))
  const startItem = totalItems === 0 ? 0 : (currentPage - 1) * pageSize + 1
  const endItem = Math.min(totalItems, currentPage * pageSize)

  if (totalItems === 0) return null

  return (
    <div className={`table-pagination ${compact ? 'compact' : ''}`}>
      <div className="pagination-info">
        {compact ? (
          <span className="mono dim">
            {startItem}–{endItem} of {totalItems}
          </span>
        ) : (
          <span className="mono dim">
            Showing <strong className="txt-white">{startItem}–{endItem}</strong> of{' '}
            <strong className="txt-white">{totalItems}</strong>
          </span>
        )}
      </div>

      <div className="pagination-controls">
        {onPageSizeChange && !compact ? (
          <div className="page-size-selector">
            <span className="dim">Rows:</span>
            {pageSizeOptions.map((opt) => (
              <button
                key={opt}
                type="button"
                className={`size-btn ${pageSize === opt ? 'active' : ''}`}
                onClick={() => {
                  onPageSizeChange(opt)
                  onPageChange(1)
                }}
              >
                {opt}
              </button>
            ))}
          </div>
        ) : null}

        <div className="page-buttons">
          <button
            type="button"
            className="page-nav-btn"
            disabled={currentPage <= 1}
            onClick={() => onPageChange(currentPage - 1)}
          >
            ‹ Prev
          </button>

          <span className="mono page-num">
            {currentPage} / {totalPages}
          </span>

          <button
            type="button"
            className="page-nav-btn"
            disabled={currentPage >= totalPages}
            onClick={() => onPageChange(currentPage + 1)}
          >
            Next ›
          </button>
        </div>
      </div>
    </div>
  )
}
