// The account's customer addresses for `/activity`'s Customers tab (`GET /api/v1/static-deposit-addresses`, PLAN.md
// section 17): the first page, loaded once and again with each debounced reload after live events, older pages on
// "Load older".
import { useCallback, useEffect, useState } from "react"

import { describeError, fetchApiJson } from "@/lib/api_client"
import type { StaticDepositAddress, StaticDepositAddressList } from "@/lib/api_types"

const staticDepositAddressesPath = "/api/v1/static-deposit-addresses"

// The reloaded first page over what is shown, by id, the older pages' rows kept; newest first like the server.
function mergeStaticDepositAddressesNewestFirst(shownAddresses: StaticDepositAddress[], loadedAddresses: StaticDepositAddress[]): StaticDepositAddress[] {
  const addressById = new Map(shownAddresses.map((staticDepositAddress) => [staticDepositAddress.id, staticDepositAddress]))
  for (const loadedAddress of loadedAddresses) addressById.set(loadedAddress.id, loadedAddress)
  return [...addressById.values()].sort((first, second) => second.created_at.localeCompare(first.created_at) || second.id.localeCompare(first.id))
}

export function useAccountStaticDepositAddresses(liveReloadCount: number) {
  const [staticDepositAddresses, setStaticDepositAddresses] = useState<StaticDepositAddress[] | null>(null)
  const [addressLoadError, setAddressLoadError] = useState("")
  const [olderAddressesCursor, setOlderAddressesCursor] = useState<string | null>(null)
  const [olderAddressesAreLoading, setOlderAddressesAreLoading] = useState(false)
  const [olderAddressesLoadError, setOlderAddressesLoadError] = useState("")

  // The state changes in the promise callbacks, once the API answers, never synchronously in the effect below.
  const loadFirstPage = useCallback(
    () =>
      fetchApiJson<StaticDepositAddressList>(staticDepositAddressesPath).then(
        (addressList) => {
          setStaticDepositAddresses((currentAddresses) =>
            currentAddresses === null ? addressList.static_deposit_addresses : mergeStaticDepositAddressesNewestFirst(currentAddresses, addressList.static_deposit_addresses),
          )
          setOlderAddressesCursor((currentCursor) => currentCursor ?? addressList.next_cursor)
          setAddressLoadError("")
        },
        (loadError: unknown) => setAddressLoadError(describeError(loadError)),
      ),
    [],
  )
  useEffect(() => {
    void loadFirstPage()
  }, [loadFirstPage, liveReloadCount])

  async function loadOlderAddresses() {
    if (olderAddressesCursor === null || olderAddressesAreLoading) return
    setOlderAddressesAreLoading(true)
    try {
      const addressList = await fetchApiJson<StaticDepositAddressList>(`${staticDepositAddressesPath}?cursor=${encodeURIComponent(olderAddressesCursor)}`)
      setStaticDepositAddresses((currentAddresses) => mergeStaticDepositAddressesNewestFirst(currentAddresses ?? [], addressList.static_deposit_addresses))
      setOlderAddressesCursor(addressList.next_cursor)
      setOlderAddressesLoadError("")
    } catch (loadError: unknown) {
      setOlderAddressesLoadError(describeError(loadError))
    } finally {
      setOlderAddressesAreLoading(false)
    }
  }

  return {
    staticDepositAddresses,
    addressLoadError,
    hasOlderAddresses: olderAddressesCursor !== null,
    olderAddressesAreLoading,
    olderAddressesLoadError,
    loadOlderAddresses,
  }
}
