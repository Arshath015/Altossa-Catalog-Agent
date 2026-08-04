import React, { useState } from 'react';
import ReactDOM from 'react-dom/client';
import BrandSidebar from './BrandSidebar';
import CatalogChatWidget from './component/CatalogChatWidget';
import ImagePanel, { ImagePanelData } from './ImagePanel';

function CatalogApp() {
  const [brand, setBrand] = useState('Bolzan');
  const [latestImages, setLatestImages] = useState<ImagePanelData | null>(null);

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
          key={brand /* reset chat state cleanly when switching brands */}
          brand={brand}
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