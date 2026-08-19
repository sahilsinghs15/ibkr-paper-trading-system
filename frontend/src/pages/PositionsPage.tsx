import { useParams } from 'react-router-dom'
import { ClosedPositionsTable } from '../components/ClosedPositionsTable'
import { Kpis } from '../components/Kpis'
import { OpenPositionsTable } from '../components/OpenPositionsTable'

export function PositionsPage() {
  const { ibkrAccount } = useParams<{ ibkrAccount: string }>()
  const cleanAccount = ibkrAccount ? ibkrAccount.trim().toUpperCase() : 'DUR919062'

  return (
    <main className="page">
      <Kpis accountFilter={cleanAccount} />
      <OpenPositionsTable accountFilter={cleanAccount} />
      <ClosedPositionsTable accountFilter={cleanAccount} />
    </main>
  )
}
