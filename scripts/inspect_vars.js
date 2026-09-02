const fs = require('fs');
const html = fs.readFileSync('scripts/article_dump.html', 'utf8');

const mAlbum = html.match(/appmsg_album_infos\s*=\s*(\[[\s\S]*?\]);/);
console.log('mAlbum:', mAlbum ? mAlbum[1] : 'none');

const mCgi = html.match(/window\.cgiData\s*=\s*(\{[\s\S]*?\});/);
console.log('mCgi:', mCgi ? mCgi[1].slice(0, 300) : 'none');

const mRelated = html.match(/related_article_list\s*=\s*(\[[\s\S]*?\]);/);
console.log('mRelated:', mRelated ? mRelated[1] : 'none');

const mBiz = html.match(/var\s+biz\s*=\s*["']([^"']+)["']/);
console.log('biz:', mBiz ? mBiz[1] : 'none');
