import { createContext, useContext } from "react"

// True for the public demo account: the server refuses its writes, so the UI hides them.
export const ReadOnlyContext = createContext(false)

export const useReadOnly = (): boolean => useContext(ReadOnlyContext)
