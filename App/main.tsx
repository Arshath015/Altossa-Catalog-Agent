import React, { useState } from 'react';
import ReactDOM from 'react-dom/client';
import BrandSidebar from './BrandSidebar';
import CatalogChatWidget, { BrandSession, createInitialBrandSession } from './component/CatalogChatWidget';
import ImagePanel, { ImagePanelData } from './ImagePanel';

function CatalogApp() {
  const [brand, setBrand] = useState('Bolzan');
  const [latestImages, setLatestImages] = useState<ImagePanelData | null>(null);
  // Each brand keeps its own independent chat session (messages + anchor
  // context) so switching brands and back doesn't lose history -- keyed by
  // brand name, created lazily on first use. Anchors live inside each
  // brand's own slot, so a product resolved in one brand's conversation can
  // never be read as context for another brand's query.
  const [brandSessions, setBrandSessions] = useState<Record<string, BrandSession>>({});

  function updateBrandSession(forBrand: string, updater: (prev: BrandSession) => BrandSession) {
    setBrandSessions(prev => ({
      ...prev,
      [forBrand]: updater(prev[forBrand] ?? createInitialBrandSession(forBrand)),
    }));
  }

  return (
    <div className="riso-grain h-screen w-screen flex bg-[var(--riso-bg)] overflow-hidden">
      <BrandSidebar
        selected={brand}
        onSelect={(b) => {
          setBrand(b);
          setLatestImages(null);
        }}
      />
      <div className="flex-1 min-w-0">
        <CatalogChatWidget
          brand={brand}
          session={brandSessions[brand] ?? createInitialBrandSession(brand)}
          onSessionChange={(updater) => updateBrandSession(brand, updater)}
          onLatestImages={setLatestImages}
        />
      </div>
      <ImagePanel data={latestImages} />
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <CatalogApp />
  </React.StrictMode>
);