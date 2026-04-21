const DB_NAME = 'fta-visual-exploded-3d'
const DB_VERSION = 1
const STORE = 'bundles'

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION)
    req.onerror = () => reject(req.error)
    req.onupgradeneeded = () => {
      const db = req.result
      if (!db.objectStoreNames.contains(STORE)) {
        db.createObjectStore(STORE)
      }
    }
    req.onsuccess = () => resolve(req.result)
  })
}

/**
 * @param {string} projectId
 * @param {{ glbBlob: Blob, partDetails: Record<string, { id?: string, name?: string, type?: string }> }} bundle
 */
export async function saveExploded3dBundle(projectId, bundle) {
  if (!projectId) throw new Error('projectId required')
  const db = await openDb()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readwrite')
    tx.oncomplete = () => resolve(undefined)
    tx.onerror = () => reject(tx.error)
    const store = tx.objectStore(STORE)
    store.put(
      {
        glbBlob: bundle.glbBlob,
        partDetails: bundle.partDetails || {},
        updatedAt: Date.now(),
      },
      projectId,
    )
  })
}

/** @returns {Promise<{ glbBlob: Blob, partDetails: object } | null>} */
export async function loadExploded3dBundle(projectId) {
  if (!projectId) return null
  const db = await openDb()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readonly')
    const store = tx.objectStore(STORE)
    const req = store.get(projectId)
    req.onsuccess = () => {
      const v = req.result
      if (!v || !v.glbBlob) {
        resolve(null)
        return
      }
      resolve({
        glbBlob: v.glbBlob,
        partDetails: v.partDetails && typeof v.partDetails === 'object' ? v.partDetails : {},
      })
    }
    req.onerror = () => reject(req.error)
  })
}

export async function deleteExploded3dBundle(projectId) {
  if (!projectId) return
  const db = await openDb()
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readwrite')
    tx.oncomplete = () => resolve(undefined)
    tx.onerror = () => reject(tx.error)
    tx.objectStore(STORE).delete(projectId)
  })
}
