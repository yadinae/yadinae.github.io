// Custom Giscus Configuration
document.addEventListener('DOMContentLoaded', function() {
  // Create giscus container
  const commentContainer = document.createElement('div');
  commentContainer.id = 'giscus-wrap';
  commentContainer.style.cssText = 'margin-top: 40px; padding: 20px 0;';
  
  // Find the article container and append comment
  const articleEnd = document.querySelector('#post .post-content') || document.querySelector('.article-container');
  if (articleEnd) {
    articleEnd.parentNode.appendChild(commentContainer);
    
    // Load Giscus script
    const script = document.createElement('script');
    script.src = 'https://giscus.app/client.js';
    script.setAttribute('data-repo', 'yadinae/yadinae.github.io');
    script.setAttribute('data-repo-id', 'R_kgDORMx6OQ');
    script.setAttribute('data-category', 'General');
    script.setAttribute('data-category-id', 'DIC_kwDORMx6Oc4C3f7M');
    script.setAttribute('data-mapping', 'pathname');
    script.setAttribute('data-strict', '0');
    script.setAttribute('data-reactions-enabled', '1');
    script.setAttribute('data-emit-metadata', '0');
    script.setAttribute('data-input-position', 'bottom');
    script.setAttribute('data-theme', 'preferred_color_scheme');
    script.setAttribute('data-lang', 'zh-CN');
    script.crossOrigin = 'anonymous';
    script.async = true;
    
    commentContainer.appendChild(script);
  }
});
