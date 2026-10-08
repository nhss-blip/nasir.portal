"""One maintained list of school services; audience-specific pages are a later change."""
SERVICES = (
    dict(name='Magazines', url='/magazines', image='magazines.jpg', note='Browse school magazines', kind='School'),
    dict(name='School Announcements', url='/announcements', image='school announcements.jpg', note='Dates, news and important notices', kind='School'),
    dict(name='Al-Islam', url='https://qc.4everproxy.com/direct/aHR0cHM6Ly93d3cuYWxpc2xhbS5vcmcv', image='al-islam.jpg', note='Ahmadiyya reference resources', kind='Web'),
    dict(name='Al-Hakam', url='https://www.alhakam.org/', image='al-hakam.jpg', note='Ahmadiyya Islamic newspaper', kind='Web'),
    dict(name='ERP', url='http://192.168.88.21:8000', image='ERP.jpg', note='School management software', kind='School network'),
    dict(name='Nextcloud', url='http://192.168.88.23', image='nextcloud.avif', note='Pictures, notes and shared files', kind='School network'),
)
