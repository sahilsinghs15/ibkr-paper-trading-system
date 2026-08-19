import { DashboardHeader } from '../components/DashboardHeader'
import { Kpis } from '../components/Kpis'
import { OpenPositionsTable } from '../components/OpenPositionsTable'
import { ClosedPositionsTable } from '../components/ClosedPositionsTable'
import { usePnlStream } from '../hooks/usePnlStream'

export function PositionsPage() {
  usePnlStream()

  return (
    <>
      <DashboardHeader />
      <Kpis />
      <main>
        <OpenPositionsTable />
        <ClosedPositionsTable />
      </main>
    </>
  )
}
